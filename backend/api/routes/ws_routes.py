"""
WebSocket endpoint for real-time market data streaming.

Reads the shared JSON state file (written by paper_trader at 200ms intervals)
and pushes updates to connected clients. Replaces client-side HTTP polling.
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

_STATE_FILE = os.environ.get("LIVE_STATE_FILE", "/tmp/algo_live_state.json")


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            try:
                self.active.remove(ws)
            except ValueError:
                pass


manager = ConnectionManager()


def _read_state() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"ts_ms": None, "symbols": {}, "positions": {}}


@router.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await manager.connect(ws)
    last_ts = None
    try:
        while True:
            state = _read_state()
            current_ts = state.get("ts_ms")
            if current_ts != last_ts:
                await ws.send_json(state)
                last_ts = current_ts
            await asyncio.sleep(0.15)
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)
