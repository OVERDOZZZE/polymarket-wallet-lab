"""FastAPI server: JSON API under /api, the trade websocket at /ws/trade, and the built React app at /.

Start with:  python -m backend      (or run_app.bat)
"""
import asyncio
from contextlib import asynccontextmanager

import orjson
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .routes import bots, system, trading, traders
from .state import hub
from ..config import FRONTEND_DIST
from ..traders import store
from ..trading.gateway import gateway


class FastJSON(JSONResponse):
    """orjson: fast, and NaN/inf become null (pandas output is full of NaN)."""

    def render(self, content):
        return orjson.dumps(content, option=orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_NON_STR_KEYS)


@asynccontextmanager
async def lifespan(app):
    hub.start()       # always on: its Chainlink stream gives the Trade screen exact window open/close prices
    for t in store.list_traders():
        if t.live_autostart:
            hub.watch(t.wallet, t.id, t.asset, t.window_minutes)
    gateway.start(asyncio.get_running_loop())
    yield


app = FastAPI(title="Polymarket Wallet Lab", default_response_class=FastJSON, lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2000)
for r in (system.router, traders.router, traders.export_router, bots.router, trading.router):
    app.include_router(r)

if (FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    """Any non-API path serves the React app (client-side routing)."""
    f = FRONTEND_DIST / path
    if path and f.is_file():
        return FileResponse(f)
    index = FRONTEND_DIST / "index.html"
    if index.exists():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return FastJSON({"error": "frontend not built - run: cd frontend && npm run build"}, status_code=503)
