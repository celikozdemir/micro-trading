from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException

from backend.config import load_trading_config

router = APIRouter()

CONFIG_PATH = Path("configs/default.yaml")


def _deep_merge(base: dict, updates: dict) -> dict:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


@router.get("/config")
async def get_config():
    return load_trading_config(CONFIG_PATH)


@router.put("/config")
async def update_config(updates: dict[str, Any]):
    try:
        config = load_trading_config(CONFIG_PATH)
        _deep_merge(config, updates)
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        return config
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
