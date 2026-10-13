"""
Hotspot Caps sync server.

    uvicorn app:app --host 0.0.0.0 --port $PORT

Environment:
    DATABASE_URL     Postgres connection string (omit for a local SQLite file, tests only)
    AGENT_KEY        secret the laptops send; sync is refused until this is set
    ADMIN_PASSWORD   password for the admin page (user name: admin); the page is refused until set
    ENFORCED_GATEWAYS  optional: comma-separated router MACs capped by default (default: the Huawei B310s)
"""
import base64
import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import ledger
from db import DB

MAX_BODY = 4_000_000
PAGE = (Path(__file__).parent / "admin.html").read_text(encoding="utf-8")

app = FastAPI(title="Hotspot Caps", docs_url=None, redoc_url=None, openapi_url=None)
_db = None


def db():
    global _db
    if _db is None:
        _db = DB()
        ledger.init(_db)
    return _db


def _equal(a, b):
    return hmac.compare_digest(a.encode(), b.encode())


def need_agent(request):
    key = os.environ.get("AGENT_KEY")
    if not key:
        raise HTTPException(503, "AGENT_KEY is not set on the server")
    if not _equal(request.headers.get("authorization", ""), f"Bearer {key}"):
        raise HTTPException(401, "bad key")


def need_admin(request, write=False):
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        raise HTTPException(503, "ADMIN_PASSWORD is not set on the server")
    ok = False
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            user, _, given = base64.b64decode(header[6:]).decode().partition(":")
            ok = _equal(user, "admin") and _equal(given, password)
        except Exception:
            ok = False
    if not ok:
        raise HTTPException(401, "login required", headers={"WWW-Authenticate": 'Basic realm="Hotspot Caps"'})
    # Browsers attach saved Basic credentials to requests from any site. Other sites can't add a
    # custom header, so requiring one on changes stops them from acting as the admin.
    if write and request.headers.get("x-wifimon") != "1":
        raise HTTPException(403, "missing header")


async def read_json(request):
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        raise HTTPException(413, "too large")
    try:
        data = json.loads(await request.body() or b"{}")
    except ValueError:
        raise HTTPException(400, "bad json")
    if not isinstance(data, dict):
        raise HTTPException(400, "bad json")
    return data


@app.get("/healthz")
def healthz():
    """For Render's health check and for keeping the server awake. Reports whether the database answers."""
    try:
        with db().tx() as tx:
            tx.execute("SELECT 1")
        return {"ok": True, "db": db().kind}
    except Exception as e:
        return JSONResponse({"ok": False, "error": type(e).__name__}, status_code=503)


@app.post("/api/sync")
async def sync(request: Request):
    need_agent(request)
    return ledger.sync(db(), await read_json(request))


@app.get("/", response_class=HTMLResponse)
def page(request: Request):
    need_admin(request)
    return PAGE


@app.get("/api/state")
def state(request: Request, network: str = ""):
    need_admin(request)
    return ledger.admin_view(db(), network or None)


@app.post("/api/admin/{kind}")
async def admin(kind: str, request: Request):
    need_admin(request, write=True)
    if not ledger.admin_op(db(), kind, await read_json(request)):
        raise HTTPException(400, "bad request")
    return {"ok": True}
