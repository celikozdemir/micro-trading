from fastapi import APIRouter, Request

router = APIRouter()


def _mgr(request: Request):
    return request.app.state.runner_manager


@router.get("/runner/status")
async def status(request: Request):
    return _mgr(request).status()


@router.post("/runner/start")
async def start(request: Request):
    await _mgr(request).start()
    return {"ok": True, "running": True}


@router.post("/runner/stop")
async def stop(request: Request):
    await _mgr(request).stop()
    return {"ok": True, "running": False}
