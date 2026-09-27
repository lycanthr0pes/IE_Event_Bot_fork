"""イベント同期の再試行期限・回数・隔離を、キュー項目と一緒に保存する。"""

import time
from copy import deepcopy

RETRY_FIELD = "_sync_retry"
QUEUE_KEYS = {
    "discord": "sync:discord_notion_queue",
    "google": "sync:google_apply_queue",
}


def retry_now() -> int:
    return int(time.time())


def _setting(env, name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(getattr(env, name, default)))
    except (ValueError, TypeError):
        return default
    return value if minimum <= value <= maximum else default


def retry_metadata(item: dict) -> dict:
    value = item.get(RETRY_FIELD, {})
    if not isinstance(value, dict):
        raise RuntimeError("sync_retry_state_invalid")
    for key in ("attempts", "next_attempt_at", "quarantined_at", "generation"):
        number = value.get(key, 0)
        if type(number) is not int or number < 0:
            raise RuntimeError("sync_retry_state_invalid")
    return value


def is_quarantined(item: dict) -> bool:
    return bool(retry_metadata(item).get("quarantined_at"))


def select_retry_items(items: list[dict], limit: int) -> tuple[list[dict], list[dict]]:
    """待機中・隔離中の項目は処理枠を消費しない。"""
    now = retry_now()
    selected, remaining = [], []
    for item in items:
        retry = retry_metadata(item)
        if (len(selected) < limit and not is_quarantined(item)
                and retry.get("next_attempt_at", 0) <= now):
            selected.append(item)
        else:
            remaining.append(item)
    return selected, remaining


def record_retry_failure(env, item: dict, error: str) -> dict:
    """初回を含め6回失敗したら隔離する。例外本文は保存しない。"""
    retry = retry_metadata(item)
    attempts = retry.get("attempts", 0) + 1
    maximum = _setting(env, "SYNC_EVENT_MAX_RETRIES", 5, 0, 20)
    delay = _setting(env, "SYNC_EVENT_RETRY_SECONDS", 300, 1, 86400)
    now = retry_now()
    return {**deepcopy(item), RETRY_FIELD: {
        "attempts": attempts,
        "next_attempt_at": now + delay,
        "quarantined_at": now if attempts >= maximum + 1 else 0,
        "generation": retry.get("generation", 0),
        "last_error": error,
    }}


def merge_retry_metadata(first: dict, second: dict) -> dict:
    """古いsnapshotで回数・期限・明示再投入を巻き戻さない。"""
    candidates = [retry_metadata(first), retry_metadata(second)]
    return deepcopy(max(candidates, key=lambda retry: (
        retry.get("generation", 0), retry.get("attempts", 0),
        retry.get("next_attempt_at", 0), retry.get("quarantined_at", 0),
    )))


def retry_counts(items: list[dict]) -> tuple[int, int]:
    quarantined = sum(is_quarantined(item) for item in items)
    return len(items) - quarantined, quarantined


def has_failed_items(items: list[dict]) -> bool:
    return any(retry_metadata(item).get("attempts", 0) > 0 for item in items)
