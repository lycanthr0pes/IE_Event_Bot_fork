"""Cloudflare Python Workers の最小ローカルランタイムを用意する。"""

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


SOURCE_DIR = Path(__file__).resolve().parents[1] / "workers" / "src"
sys.path.insert(0, str(SOURCE_DIR))


class Response:
    """テストで必要な Workers Response の最小実装。"""

    def __init__(
        self,
        body: Any = "",
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._body = body
        self.status = status
        self.headers = headers or {}

    async def text(self) -> str:
        if isinstance(self._body, bytes):
            return self._body.decode("utf-8")
        return str(self._body or "")

    async def json(self) -> Any:
        return json.loads(await self.text())


class WorkerEntrypoint:
    """本番クラスを import するための基底クラス。"""

    env: Any


class DurableObject:
    """本番クラスを import するための基底クラス。"""

    ctx: Any
    env: Any


async def blocked_fetch(url: str, *args: Any, **kwargs: Any) -> Any:
    """意図しない外部 API 接続をテスト失敗にする。"""
    raise AssertionError(f"外部通信は禁止されています: {url}")


workers_runtime = ModuleType("workers")
setattr(workers_runtime, "Response", Response)
setattr(workers_runtime, "WorkerEntrypoint", WorkerEntrypoint)
setattr(workers_runtime, "DurableObject", DurableObject)
setattr(workers_runtime, "fetch", blocked_fetch)
sys.modules["workers"] = workers_runtime


@pytest.fixture
def spaced_sync_runs(monkeypatch):
    """既存の復旧テストでは同期の呼出し間を5分進め、実時間を待たない。"""
    import discord_notion_sync
    import google_apply_sync
    import sync_retry
    import e2e_sync_retry

    now = [20000]
    select = sync_retry.select_retry_items

    def next_run(items, limit):
        now[0] += 300
        return select(items, limit)

    async def advance(delay):
        now[0] += int(delay)

    monkeypatch.setattr(sync_retry, "retry_now", lambda: now[0])
    monkeypatch.setattr(e2e_sync_retry, "retry_now", lambda: now[0])
    monkeypatch.setattr(e2e_sync_retry, "sleep", advance)
    monkeypatch.setattr(discord_notion_sync, "select_retry_items", next_run)
    monkeypatch.setattr(google_apply_sync, "select_retry_items", next_run)
