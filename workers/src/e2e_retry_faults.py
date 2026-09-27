"""所有KVと実DOで再試行を検査する。外部通知は固定の失敗モデルを使う。"""

import json
from types import SimpleNamespace

from discord_notion_sync import _apply_discord_event_diff
from e2e_sync_retry import wait_before_retry
from entry import Application
from sync_retry import RETRY_FIELD, retry_metadata
from sync_retry_admin import load_retry_items


async def retry_case(kv):
    # 本番と同じ既定300秒を検査し、上限検査だけ実時間1秒へ短縮する。
    env = SimpleNamespace(
        STATE_KV=kv, SYNC_COORDINATOR=kv.store.env.SYNC_COORDINATOR,
        SYNC_DO_LOCK_ENABLED="true", INTERNAL_API_TOKEN="owned-retry-probe",
        DISCORD_TO_GOOGLE_SYNC_ENABLED="false", DISCORD_NOTION_MAX_CHANGES_PER_RUN="1",
        EVENT_CREATE_CHANNEL_ID="owned-channel", KV_RESULT_MIN_WRITE_SECONDS="0",
    )
    bounded = kv.case == "retry_quarantine"
    if bounded:
        env.SYNC_EVENT_RETRY_SECONDS = "1"
    events = [{
        "id": f"probe-{i}", "name": "retry probe", "status": 1,
        "scheduled_start_time": "2030-01-01T00:00:00Z",
    } for i in (1, 2)]
    applies, posts, attempts = [], [], []
    restored = False

    def require(condition):
        if not condition:
            raise RuntimeError("retry_probe_observation_changed")

    async def apply(_env, event, _token):
        applies.append(event["id"])
        return True

    async def notify(_env, event, *, delivery):
        if not delivery.get("message_id"):
            posts.append(event["id"])
            delivery["message_id"] = "owned-message-" + event["id"]
        if event["id"] == "probe-1":
            attempts.append(event["id"])
            return restored
        return True

    async def forbidden(*args):
        raise RuntimeError("retry_probe_delete_forbidden")

    async def poll():
        if bounded:
            await wait_before_retry(kv.state(), ("discord",))
        return await _apply_discord_event_diff(
            env, kv.state(), events, upsert_runner=apply,
            delete_runner=forbidden, notify_runner=notify,
        )

    await poll()
    await poll()
    require(applies == ["probe-1", "probe-2"] and posts == applies)
    evidence = {
        "case": kv.case, "later_event_processed": True, "notification_posts": 2,
        "external_api_called": False, "read_model": "injected",
    }
    if not bounded:
        result = await poll()
        items, _ = await load_retry_items(kv.state(), "discord")
        require(len(attempts) == 1 and result["processed_changes"] == 0)
        require(len(items) == 1 and retry_metadata(items[0])["attempts"] == 1)
        evidence.update(retry_seconds=300, attempts=1, queue_drained=False)
        return evidence

    for _ in range(5):
        await poll()
    items, _ = await load_retry_items(kv.state(), "discord")
    require(len(items) == 1 and len(attempts) == 6)
    require(retry_metadata(items[0])["attempts"] == 6 and retry_metadata(items[0])["quarantined_at"] > 0)
    result = await poll()
    require(result["processed_changes"] == 0 and result["quarantined_changes"] == 1 and len(attempts) == 6)
    saved_delivery = dict(items[0]["notification"])

    async def admin(path, method, *, authorized=True):
        async def body():
            return json.dumps({"source": "discord", "event_id": "probe-1"})

        # 通常HTTPハンドラを呼ぶが、状態はrun所有prefixだけへ限定する。
        response = await Application(env).fetch(SimpleNamespace(
            url="https://owned.invalid" + path, method=method, text=body,
            headers={"Authorization": "Bearer owned-retry-probe" if authorized else ""},
        ))
        text = await response.text()
        return response.status, json.loads(text) if text.startswith("{") else {}

    require((await admin("/admin/sync/quarantine?source=discord", "GET", authorized=False))[0] == 401)
    status, summary = await admin("/admin/sync/quarantine?source=discord", "GET")
    require(status == 200 and len(summary["events"]) == 1 and summary["events"][0]["attempts"] == 6)
    status, result = await admin("/admin/sync/requeue", "POST")
    require(status == 200 and result == {"ok": True, "requeued": True})
    items, _ = await load_retry_items(kv.state(), "discord")
    require(items[0]["notification"] == saved_delivery)
    require(items[0][RETRY_FIELD]["generation"] == 1 and items[0][RETRY_FIELD]["attempts"] == 0)
    require(await admin("/admin/sync/requeue", "POST") == (200, {"ok": True, "requeued": False}))
    restored = True
    result = await poll()
    require(result["ok"] is True and result["pending_changes"] == 0)
    require(await kv.state().get_json("sync:discord_notion_queue") == [])
    require(applies == ["probe-1", "probe-2"] and posts == applies and len(attempts) == 7)
    evidence.update(
        attempts=6, quarantine_skipped=True, requeue_idempotent=True,
        message_id_preserved=True, queue_drained=True, retry_seconds=1,
        admin_handler_verified=True,
    )
    return evidence
