"""隔離したイベントを確認し、同じ同期ロックの下で再投入する。"""

from discord_retry_state import merge_retry_ops, snapshot_with_pending, split_snapshot
from sync_retry import QUEUE_KEYS, RETRY_FIELD, is_quarantined, retry_metadata


async def load_retry_items(state, source: str) -> tuple[list[dict], dict]:
    items = await state.get_json(QUEUE_KEYS[source], [])
    if not isinstance(items, list) or any(
        not isinstance(item, dict) or not item.get("id") for item in items
    ):
        raise RuntimeError("sync_retry_queue_invalid")
    snapshot = {}
    if source == "discord":
        snapshot, pending = split_snapshot(await state.get_discord_snapshot())
        items = merge_retry_ops(pending, items)
    for item in items:
        retry_metadata(item)
    return items, snapshot


def quarantine_summary(items: list[dict]) -> list[dict]:
    """イベント本文や通知先を管理レスポンスへ含めない。"""
    return [{
        "event_id": item["id"],
        "operation": item.get("op", "apply"),
        "attempts": retry_metadata(item).get("attempts", 0),
        "quarantined_at": retry_metadata(item).get("quarantined_at", 0),
        "last_error": retry_metadata(item).get("last_error", ""),
    } for item in items if is_quarantined(item)]


async def requeue_event(state, source: str, event_id: str) -> tuple[dict, int]:
    items, snapshot = await load_retry_items(state, source)
    target = next((item for item in items if item["id"] == event_id), None)
    if target is None:
        return {"ok": False, "error": "event_not_found"}, 404
    retry = retry_metadata(target)
    if not is_quarantined(target):
        # 同じ再投入要求の再送ではカウンターを繰り返しリセットしない。
        if retry.get("generation", 0) > 0 and retry.get("attempts", 0) == 0:
            return {"ok": True, "requeued": False}, 200
        return {"ok": False, "error": "event_not_quarantined"}, 409
    target[RETRY_FIELD] = {
        **retry, "attempts": 0, "next_attempt_at": 0, "quarantined_at": 0,
        "generation": retry.get("generation", 0) + 1,
        "last_quarantined_at": retry["quarantined_at"],
    }
    # 投稿済みmessage IDや元イベントを保ったまま末尾へ再投入する。
    items = [item for item in items if item["id"] != event_id] + [target]
    await state.put_json_if_changed(QUEUE_KEYS[source], items)
    if source == "discord":
        await state.set_discord_snapshot(snapshot_with_pending(snapshot, snapshot, items))
    return {"ok": True, "requeued": True}, 200
