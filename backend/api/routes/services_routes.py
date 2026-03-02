from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.session import get_session

router = APIRouter()

_SERVICES = [
    {"name": "algo-recorder", "display": "Data Recorder"},
    {"name": "algo-paper",    "display": "Paper Trader"},
]

# Only these services can be controlled via the API
_CONTROLLABLE = {"algo-recorder", "algo-paper"}


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
            line = ts_result.stdout.strip()
            if "=" in line:
                val = line.split("=", 1)[1].strip()
                try:
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
        return False, None


def _systemctl_control(service: str, action: str) -> tuple[bool, str]:
    """Run sudo systemctl {action} {service}. Returns (success, error_message)."""
    try:
        result = subprocess.run(
            ["sudo", "systemctl", action, service],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return True, ""
        return False, (result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


@router.get("/services")
async def get_services(session: AsyncSession = Depends(get_session)):
    tasks = [asyncio.to_thread(_systemctl_active, svc["name"]) for svc in _SERVICES]
    service_states = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for svc, state in zip(_SERVICES, service_states):
        active, uptime_s = (False, None) if isinstance(state, Exception) else state
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


@router.post("/services/{name}/{action}")
async def control_service(name: str, action: str):
    if name not in _CONTROLLABLE:
        raise HTTPException(status_code=403, detail=f"Service '{name}' is not controllable via API")
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail=f"Unknown action '{action}'. Use start, stop, or restart.")

    ok, err = await asyncio.to_thread(_systemctl_control, name, action)
    if not ok:
        raise HTTPException(status_code=500, detail=err or f"Failed to {action} {name}")

    # Brief wait then return fresh status
    await asyncio.sleep(1.5)
    active, uptime_s = await asyncio.to_thread(_systemctl_active, name)
    return {"ok": True, "action": action, "service": name, "active": active}
