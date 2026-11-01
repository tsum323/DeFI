"""Dashboard API (FastAPI), read-only. Serves the built frontend from frontend/dist.

  uvicorn backend.api.app:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api import queries as Q
from backend.core.config import load_config
from backend.database import schema as S

ROOT = Path(__file__).resolve().parents[2]
cfg = load_config()
engine = S.make_engine(cfg.lab["database"]["url"])
app = FastAPI(title="DeFi Liquidity Lab", version="0.10", docs_url="/api/docs")


@app.get("/api/overview")
def overview():
    return Q.overview(engine, cfg)


@app.get("/api/strategies")
def strategies():
    return Q.ranking(engine, cfg)


@app.get("/api/strategies/{spec_hash}")
def strategy(spec_hash: str):
    d = Q.strategy_details(engine, cfg, spec_hash)
    if d is None:
        raise HTTPException(404, "unknown strategy")
    return d


@app.get("/api/research")
def research():
    return Q.research(engine)


@app.get("/api/signals")
def signals():
    return Q.signals(engine)


@app.get("/api/portfolio")
def portfolio():
    return Q.portfolio(engine)


@app.get("/api/datasources")
def datasources():
    return Q.datasources(engine, cfg)


DIST = ROOT / "frontend" / "dist"
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    index = DIST / "index.html"
    if path.startswith("api/") or not index.exists():
        raise HTTPException(404, "not found (frontend not built: cd frontend && npm run build)")
    return FileResponse(index)
