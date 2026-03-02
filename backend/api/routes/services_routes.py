from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.session import get_session

router = APIRouter()

_SERVICES = [
    {"name": "algo-recorder", "display": "Data Recorder"},
    {"name": "algo-paper",    "display": "Paper Trader"},
]


def _systemctl_active(service: str) -> tuple[bool, int | None]:
    """Return (is_active, uptime_seconds). Runs synchronously — call via to_thread."""
    try:
        active_result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=3,
        )
        is_active = active_result.stdout.strip() == "active"

        uptime_s: int | None = None
        if is_active:
            ts_result = subprocess.run(
                ["systemctl", "show", service, "--property=ActiveEnterTimestamp"],
                capture_output=True, text=True, timeout=3,
            )
            # Output: "ActiveEnterTimestamp=Mon 2026-03-02 17:12:22 UTC"
            line = ts_result.stdout.strip()
            if "=" in line:
                val = line.split("=", 1)[1].strip()
                try:
                    # Format: "Mon 2026-03-02 17:12:22 UTC"
                    parts = val.split()
                    if len(parts) >= 3:
                        dt = datetime.strptime(
                            f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=timezone.utc)
                        uptime_s = int((datetime.now(timezone.utc) - dt).total_seconds())
                except Exception:
                    pass

        return is_active, uptime_s

    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # systemd not available (dev machine / macOS)
        return False, None


@router.get("/services")
async def get_services(session: AsyncSession = Depends(get_session)):
    results = []

    # Check systemd services concurrently
    tasks = [
        asyncio.to_thread(_systemctl_active, svc["name"])
        for svc in _SERVICES
    ]
    service_states = await asyncio.gather(*tasks, return_exceptions=True)

    for svc, state in zip(_SERVICES, service_states):
        if isinstance(state, Exception):
            active, uptime_s = False, None
        else:
            active, uptime_s = state

        entry: dict = {"name": svc["name"], "display": svc["display"], "active": active}
        if uptime_s is not None:
            entry["uptime_s"] = uptime_s
        results.append(entry)

    # Database check
    db_ok = False
    try:
        await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    results.append({"name": "database", "display": "Database", "active": db_ok})

    return results
