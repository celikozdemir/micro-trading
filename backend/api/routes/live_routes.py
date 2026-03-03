"""
Live market state endpoint.

Reads the shared JSON file written by workers/paper_trader.py every 200ms.
Returns current bid/ask/mid prices per symbol plus any open position state.
Returns a safe empty response if the paper trader is not running.
"""

from __future__ import annotations

import json
import os

from fastapi import APIRouter

router = APIRouter()

_STATE_FILE = os.environ.get("LIVE_STATE_FILE", "/tmp/algo_live_state.json")


@router.get("/live")
async def get_live_state():
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"ts_ms": None, "symbols": {}, "positions": {}}
