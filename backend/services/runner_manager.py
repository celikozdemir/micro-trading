"""
RunnerManager — controls the MicroRunner as a background asyncio task.
Stored on app.state so all routes share one instance.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from backend.config import load_trading_config
from workers.micro_runner import MicroRunner, init_db


class RunnerManager:
    def __init__(self) -> None:
        self._runner: Optional[MicroRunner] = None
        self._task: Optional[asyncio.Task] = None
        self._started_at: Optional[datetime] = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            return
        await init_db()
        config = load_trading_config()
        self._runner = MicroRunner(config)
        self._task = asyncio.create_task(self._runner.run())
        self._started_at = datetime.now(timezone.utc)

    async def stop(self) -> None:
        if not self.is_running or self._runner is None:
            return
        await self._runner.stop()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self._runner = None
        self._started_at = None

    def status(self) -> dict:
        if not self.is_running or self._runner is None:
            return {
                "running": False,
                "uptime_s": 0,
                "total_book_ticks": 0,
                "total_agg_trades": 0,
                "buffer_book_ticks": 0,
                "buffer_agg_trades": 0,
            }
        uptime = (
            (datetime.now(timezone.utc) - self._started_at).total_seconds()
            if self._started_at
            else 0
        )
        return {"running": True, "uptime_s": int(uptime), **self._runner.get_stats()}
