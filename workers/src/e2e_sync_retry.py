"""所有済みE2Eの再試行を短い実時間で待つ。通常同期の期限判定は変更しない。"""

from asyncio import sleep

from sync_retry import QUEUE_KEYS, is_quarantined, retry_metadata, retry_now
from sync_retry_admin import load_retry_items


async def wait_before_retry(state, sources=("google", "discord")):
    deadlines = []
    for source in sources:
        if source == "discord":
            items, _ = await load_retry_items(state, source)
        else:
            items = await state.get_json(QUEUE_KEYS[source], []) or []
        for item in items:
            if not is_quarantined(item):
                deadlines.append(retry_metadata(item).get("next_attempt_at", 0))
    delay = max(deadlines, default=0) - retry_now()
    if delay > 5:
        # 旧runや別設定を勝手に短縮せず、期限後の再呼出しを要求する。
        raise RuntimeError("e2e_sync_retry_not_due")
    if delay > 0:
        await sleep(delay)
