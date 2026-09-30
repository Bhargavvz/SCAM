"""Meridian - supply chain management system. FastAPI application: API under /api, the React app everywhere else."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import ROOT, settings
from app.db import TODAY, connect
from app.routers import catalog, insight, operations, procurement, sales
from app.services import alerts as alert_engine
from app.services import llm

app = FastAPI(title="Meridian SCM", version="1.0.0")
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

AUTH = bool(settings.ui_user and settings.ui_password)
SECRET = (settings.session_secret or secrets.token_hex(32)).encode()
COOKIE = "meridian_session"


def _sign(user: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"u": user, "t": int(time.time())}).encode()).decode()
    return payload + "." + hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()


def _user(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()):
        return None
    data = json.loads(base64.urlsafe_b64decode(payload))
    return data["u"] if time.time() - data["t"] < 7 * 86400 else None


@app.middleware("http")
async def auth(request: Request, call_next):
    p = request.url.path
    if AUTH and p.startswith("/api/") and p not in ("/api/session", "/api/login", "/api/health") \
            and not _user(request.cookies.get(COOKIE)):
        return JSONResponse({"detail": "Please sign in."}, status_code=401)
    return await call_next(request)


class Login(BaseModel):
    username: str
    password: str


@app.get("/api/session")
def session(request: Request):
    return {"auth_required": AUTH, "user": _user(request.cookies.get(COOKIE)) if AUTH else "planner"}


@app.post("/api/login")
def login(body: Login):
    if not AUTH:
        return {"ok": True, "user": "planner"}
    if not (hmac.compare_digest(body.username, settings.ui_user) and hmac.compare_digest(body.password, settings.ui_password)):
        time.sleep(0.5)
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    r = JSONResponse({"ok": True, "user": body.username})
    r.set_cookie(COOKIE, _sign(body.username), httponly=True, samesite="lax", max_age=7 * 86400)
    return r


@app.post("/api/logout")
def logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE)
    return r


@app.get("/api/health")
def health():
    return {"ok": settings.db_path.exists(), "business_date": TODAY, "ai": llm.available(), "model": settings.groq_model}


@app.on_event("startup")
def startup():
    con = connect()
    try:
        print("alerts:", alert_engine.refresh(con))
    finally:
        con.close()


for r in (catalog.router, procurement.router, sales.router, operations.router, insight.router):
    app.include_router(r)

DIST = ROOT / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(status_code=404)
        f = DIST / path
        return FileResponse(f if path and f.is_file() else DIST / "index.html")
