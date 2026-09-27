"""失敗イベントが後続通知を妨げないことと、再試行の境界を検証する。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

import discord_notion_sync as discord
import google_apply_sync as google
import sync_retry
from discord_retry_state import merge_retry_ops
from entry import Default
from state import StateStore
from sync_retry_admin import requeue_event
from tests.fakes import MemoryKV, Request, make_sync_coordinator_namespace


def run(coro):
    return asyncio.run(coro)


def environment(limit=2):
    return SimpleNamespace(
        STATE_KV=MemoryKV(), DISCORD_TO_GOOGLE_SYNC_ENABLED="false",
        DISCORD_NOTION_MAX_CHANGES_PER_RUN=str(limit),
        EVENT_CREATE_CHANNEL_ID="channel",
    )


@pytest.fixture
def clock(monkeypatch):
    now = [10000]
    monkeypatch.setattr(sync_retry, "retry_now", lambda: now[0])
    return now


@pytest.mark.parametrize("raises", [False, True])
def test_failed_active_event_does_not_block_tomorrow(raises):
    env = environment(1 if not raises else 2)
    events = [{"id": "active", "status": 2}, {"id": "tomorrow", "status": 1}]
    notified = []

    async def apply(_env, event, token):
        if event["id"] == "active":
            if raises:
                raise RuntimeError("injected failure")
            return False
        return True

    async def notify(_env, event, *, delivery):
        notified.append(event["id"])
        return True

    for _ in range(2):
        run(discord._apply_discord_event_diff(
            env, StateStore(env), events, upsert_runner=apply, notify_runner=notify,
        ))
    assert notified == ["tomorrow"]


def test_immediate_manual_or_webhook_run_does_not_retry():
    env = environment()
    calls = []

    async def fail(_env, event, token):
        calls.append(event["id"])
        return False

    for _ in range(5):
        run(discord._apply_discord_event_diff(
            env, StateStore(env), [{"id": "active"}], upsert_runner=fail,
        ))
    assert calls == ["active"]


def test_six_failures_quarantine_without_losing_notification(clock):
    env = environment()
    calls = []

    async def fail(_env, event, token):
        calls.append(event["id"])
        raise RuntimeError("secret-like exception text must not be saved")

    def step():
        return run(discord._apply_discord_event_diff(
            env, StateStore(env), [{"id": "active"}], upsert_runner=fail,
        ))

    assert step()["pending_changes"] == 1
    clock[0] += 299
    assert step()["processed_changes"] == 0
    for attempt in range(2, 7):
        clock[0] = 10000 + (attempt - 1) * 300
        result = step()
        assert len(calls) == attempt
    assert result["pending_changes"] == 0
    assert result["quarantined_changes"] == 1
    saved = run(StateStore(env).get_json(sync_retry.QUEUE_KEYS["discord"]))
    assert isinstance(saved, list)
    assert saved[0]["notification"] == {"channel_id": "channel"}
    assert saved[0][sync_retry.RETRY_FIELD]["attempts"] == 6
    assert "secret-like" not in json.dumps(saved)
    clock[0] += 86400
    assert step()["processed_changes"] == 0
    # 一覧から消えた隔離イベントも調査・再投入用に保持する。
    run(discord._apply_discord_event_diff(env, StateStore(env), []))
    assert run(StateStore(env).get_json(sync_retry.QUEUE_KEYS["discord"])) == saved


def test_notification_exception_retains_message_and_retries_only_reaction(clock, monkeypatch):
    from tests.test_discord_notification_retry import poll, prepare

    env, events, calls, failures = prepare(monkeypatch)
    original = discord._discord_add_reaction

    async def fail(*args):
        raise RuntimeError("reaction transport failure")

    monkeypatch.setattr(discord, "_discord_add_reaction", fail)
    assert poll(env)["ok"] is False
    queue = run(StateStore(env).get_json(sync_retry.QUEUE_KEYS["discord"]))
    assert isinstance(queue, list)
    assert queue[0]["op"] == "notify"
    assert queue[0]["notification"]["message_id"] == "message-1"
    clock[0] += 300
    monkeypatch.setattr(discord, "_discord_add_reaction", original)
    assert poll(env)["ok"] is True
    assert len(calls["post"]) == 1 and len(calls["upsert"]) == 1


@pytest.mark.parametrize("source", ["discord", "google"])
def test_authenticated_requeue_preserves_payload_and_is_idempotent(source, clock):
    env = environment()
    env.INTERNAL_API_TOKEN = "test-token"
    env.SYNC_COORDINATOR = make_sync_coordinator_namespace()
    env.SYNC_DO_LOCK_ENABLED = "true"
    item = {"id": "event", "op": "notify", "notification": {
        "channel_id": "channel", "message_id": "already-posted",
    }} if source == "discord" else {"id": "event", "summary": "source event"}
    for _ in range(6):
        item = sync_retry.record_retry_failure(env, item, "injected_failure")
    run(StateStore(env).put_json(sync_retry.QUEUE_KEYS[source], [item]))
    worker = Default()
    worker.env = env
    headers = {"Authorization": "Bearer test-token"}
    url = "https://bot.test/admin/sync/requeue"
    payload = json.dumps({"source": source, "event_id": "event"})
    assert run(worker.fetch(Request(url, method="POST", body=payload))).status == 401
    assert run(worker.fetch(Request(url, headers=headers))).status == 405
    response = run(worker.fetch(Request(
        f"https://bot.test/admin/sync/quarantine?source={source}", headers=headers,
    )))
    summary = run(response.json())
    assert summary["events"][0]["attempts"] == 6
    assert "already-posted" not in json.dumps(summary)
    assert "source event" not in json.dumps(summary)
    response = run(worker.fetch(Request(url, method="POST", headers=headers, body=payload)))
    assert response.status == 200 and run(response.json())["requeued"] is True
    queue = run(StateStore(env).get_json(sync_retry.QUEUE_KEYS[source]))
    assert isinstance(queue, list)
    restored = queue[0]
    assert restored[sync_retry.RETRY_FIELD]["attempts"] == 0
    assert restored[sync_retry.RETRY_FIELD]["generation"] == 1
    assert {k: v for k, v in restored.items() if k != sync_retry.RETRY_FIELD} == {
        k: v for k, v in item.items() if k != sync_retry.RETRY_FIELD
    }
    again = run(worker.fetch(Request(url, method="POST", headers=headers, body=payload)))
    assert run(again.json())["requeued"] is False
    if source == "discord":
        # queueだけ新しくsnapshotが古い場合にも明示再投入を巻き戻さない。
        merged = merge_retry_ops([item], [restored])[0]
        assert merged[sync_retry.RETRY_FIELD]["generation"] == 1
        assert not sync_retry.is_quarantined(merged)


def test_requeue_cannot_race_normal_sync(clock):
    env = environment()
    env.INTERNAL_API_TOKEN = "test-token"
    env.SYNC_COORDINATOR = make_sync_coordinator_namespace()
    env.SYNC_DO_LOCK_ENABLED = "true"
    worker = Default()
    worker.env = env

    async def scenario():
        lock = await worker._acquire_sync_lock(source="normal-sync")
        try:
            return await worker.fetch(Request(
                "https://bot.test/admin/sync/requeue", method="POST",
                headers={"Authorization": "Bearer test-token"},
                body=json.dumps({"source": "discord", "event_id": "event"}),
            ))
        finally:
            await worker._release_sync_lock(lock["owner"])

    assert run(scenario()).status == 409
    assert env.STATE_KV.put_calls == []


def test_google_retry_yields_caps_and_preserves_latest_event(clock, monkeypatch):
    env = environment()
    env.NOTION_TOKEN = "test-token"
    env.NOTION_EVENT_INTERNAL_ID = "db"
    env.DISCORD_TOKEN = "test-token"
    env.DISCORD_GUILD_ID = "guild"
    env.GOOGLE_APPLY_MAX_EVENTS_PER_RUN = "1"
    calls = []
    failed = True

    async def page(*args):
        return {"id": "page", "properties": {}}

    async def update(*args, **kwargs):
        return True

    async def apply(_env, event, *args):
        calls.append(event["id"])
        return None if failed and event["id"] == "bad" else "discord-id"

    monkeypatch.setattr(google, "_notion_query_by_google_event_id", page)
    monkeypatch.setattr(google, "_notion_get_page", page)
    monkeypatch.setattr(google, "_notion_update_event", update)

    def event(event_id):
        return {"id": event_id, "summary": "original", "start": {
            "dateTime": "2099-01-01T00:00:00Z",
        }, "end": {"dateTime": "2099-01-01T01:00:00Z"}}

    def step(events):
        return run(google.apply_google_events(
            env, StateStore(env), events, discord_syncer=apply,
        ))

    step([event("bad"), event("good")])
    queue = run(StateStore(env).get_json(sync_retry.QUEUE_KEYS["google"]))
    assert isinstance(queue, list)
    assert [item["id"] for item in queue] == ["good", "bad"]
    step([])
    assert calls == ["bad", "good"]
    assert step([])["processed"] == 0
    for _ in range(5):
        clock[0] += 300
        result = step([])
    assert result["quarantined_events"] == 1
    assert result["pending_events"] == 0
    assert calls.count("bad") == 6
    changed = {**event("bad"), "summary": "corrected"}
    assert step([changed])["processed"] == 0
    queue = run(StateStore(env).get_json(sync_retry.QUEUE_KEYS["google"]))
    assert isinstance(queue, list)
    assert queue[0]["summary"] == "corrected"
    assert run(requeue_event(StateStore(env), "google", "bad"))[1] == 200
    failed = False
    assert step([])["ok"] is True
    assert run(StateStore(env).get_json(sync_retry.QUEUE_KEYS["google"])) == []


def test_stale_snapshot_cannot_reset_deadline_or_failure_count(clock):
    from copy import deepcopy

    env = environment()
    first = sync_retry.record_retry_failure(env, {"id": "event", "op": "upsert"}, "upsert_failed")
    old = deepcopy(first)
    clock[0] += 300
    second = sync_retry.record_retry_failure(env, first, "upsert_failed")
    for snapshot, queue in [([old], [second]), ([second], [old])]:
        merged = merge_retry_ops(snapshot, queue)
        assert merged[0][sync_retry.RETRY_FIELD]["attempts"] == 2
        ready, waiting = sync_retry.select_retry_items(merged, 2)
        assert not ready and waiting == merged


def test_discord_requeue_resumes_reaction_without_posting(clock, monkeypatch):
    from tests.test_discord_notification_retry import poll, prepare

    env, events, calls, failures = prepare(monkeypatch)
    env.SYNC_EVENT_MAX_RETRIES = "0"
    failures["reaction"] = 1
    assert poll(env)["quarantined_changes"] == 1
    assert run(requeue_event(StateStore(env), "discord", "event-0"))[1] == 200
    assert poll(env)["ok"] is True
    assert len(calls["post"]) == 1
    assert calls["upsert"] == ["event-0"]
    assert len(calls["reaction"]) == 2
    assert calls["reaction"][0] == calls["reaction"][1]


@pytest.mark.parametrize("body", ['[]', '{', '{"source": []}', '{"source":"other"}', '{"source":"discord"}'])
def test_requeue_rejects_invalid_requests_without_writing(body):
    env = environment()
    env.INTERNAL_API_TOKEN = "test-token"
    worker = Default()
    worker.env = env
    response = run(worker.fetch(Request(
        "https://bot.test/admin/sync/requeue", method="POST",
        headers={"Authorization": "Bearer test-token"}, body=body,
    )))
    assert response.status == 400
    assert env.STATE_KV.put_calls == []


def test_requeue_requires_durable_lock():
    env = environment()
    env.INTERNAL_API_TOKEN = "test-token"
    worker = Default()
    worker.env = env
    response = run(worker.fetch(Request(
        "https://bot.test/admin/sync/requeue", method="POST",
        headers={"Authorization": "Bearer test-token"},
        body='{"source":"discord","event_id":"event"}',
    )))
    assert response.status == 503
    assert env.STATE_KV.put_calls == []
