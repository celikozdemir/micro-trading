from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes import health
from backend.api.routes import runner, config_routes, stats, backtest_routes
from backend.api.routes import paper_trades_routes, services_routes
from backend.services.runner_manager import RunnerManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.runner_manager = RunnerManager()
    yield
    # Graceful shutdown: stop recorder if running
    if app.state.runner_manager.is_running:
        await app.state.runner_manager.stop()


app = FastAPI(
    title="Algo Trading API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(runner.router, prefix="/api", tags=["runner"])
app.include_router(config_routes.router, prefix="/api", tags=["config"])
app.include_router(stats.router, prefix="/api", tags=["stats"])
app.include_router(backtest_routes.router, prefix="/api", tags=["backtest"])
app.include_router(paper_trades_routes.router, prefix="/api", tags=["paper-trades"])
app.include_router(services_routes.router, prefix="/api", tags=["services"])


@app.get("/")
async def root():
    return {"status": "ok", "service": "algo-trading"}
