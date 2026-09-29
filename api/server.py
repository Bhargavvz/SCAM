"""HTTP API for the web UI (web/). Serves the built React app from web/dist when present.

Run:  .venv/bin/python -m uvicorn api.server:app --port 8000      (or scripts/run_ui.sh)

Every request builds its own agent: hindsight-client's sync API must not be shared across threads, and FastAPI runs
sync endpoints in a thread pool.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import agent_core
from agent.agent_core import AgentError
from agent.capabilities import ask
from agent.config import ROOT, load_settings
from agent.corpus import load_index
from agent.llm import LLMRefusal, LLMUnavailable
from agent.scenarios import holdout_context, load_holdout
from agent.setup_data import ensure_db
from demo.run_demo import check_expectations, load_specs, run_spec

SETTINGS = load_settings()
ensure_db(SETTINGS)
UI_LIVE_DB = ROOT / "runtime" / "ui_live.sqlite"
DEMO_LIVE_DB = ROOT / "demo" / "out" / "ui_demo_live.sqlite"
WEB_DIST = ROOT / "web" / "dist"

# Deployment knobs (all optional, set in .env):
#   UI_USER / UI_PASSWORD   sign-in page + session cookie when both are set (recommended on a public host)
#   UI_SESSION_SECRET       keeps sessions valid across restarts (random per start if unset)
#   CORS_ORIGINS            comma-separated extra origins (the built UI is same-origin and needs none)
#   MAX_CONCURRENT_RUNS     model-backed requests allowed at once (default 1: protects small LLM rate limits)
#   RUN_QUEUE_TIMEOUT_S     how long a request waits for a free slot before a 503 (default 300)
UI_USER, UI_PASSWORD = os.environ.get("UI_USER", ""), os.environ.get("UI_PASSWORD", "")
RUN_SLOTS = threading.BoundedSemaphore(int(os.environ.get("MAX_CONCURRENT_RUNS", "1")))
RUN_QUEUE_TIMEOUT_S = float(os.environ.get("RUN_QUEUE_TIMEOUT_S", "300"))
CORS_ORIGINS = ["http://localhost:5173"] + [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]

app = FastAPI(title="Supply Chain Memory & Decision Agent")
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])


SESSION_COOKIE = "scm_session"
SESSION_SECRET = (os.environ.get("UI_SESSION_SECRET") or secrets.token_hex(32)).encode()
SESSION_TTL_S = int(os.environ.get("UI_SESSION_TTL_S", str(12 * 3600)))
OPEN_PATHS = {"/api/health", "/api/login", "/api/logout", "/api/session"}


def _auth_enabled() -> bool:
    return bool(UI_USER and UI_PASSWORD)


def _sign(user: str, expires: int) -> str:
    msg = f"{user}|{expires}"
    sig = hmac.new(SESSION_SECRET, msg.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{msg}|{sig}".encode()).decode()


def _session_user(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        user, expires, sig = base64.urlsafe_b64decode(token.encode()).decode().split("|")
    except ValueError:
        return None
    good = hmac.new(SESSION_SECRET, f"{user}|{expires}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good) or int(expires) < time.time():
        return None
    return user


@app.middleware("http")
async def require_login(request: Request, call_next):
    """With UI_USER/UI_PASSWORD set, every /api call except login/session/health needs a signed session cookie.
    The static UI itself is public so it can show the sign-in page."""
    path = request.url.path
    if not _auth_enabled() or not path.startswith("/api/") or path in OPEN_PATHS or _session_user(request):
        return await call_next(request)
    return JSONResponse({"detail": "Please sign in."}, status_code=401)


class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/api/session")
def session(request: Request):
    if not _auth_enabled():
        return {"auth_required": False, "user": None}
    return {"auth_required": True, "user": _session_user(request)}


@app.post("/api/login")
def login(req: LoginRequest, request: Request):
    if not _auth_enabled():
        return {"ok": True, "user": None}
    ok = secrets.compare_digest(req.username, UI_USER) and secrets.compare_digest(req.password, UI_PASSWORD)
    if not ok:
        time.sleep(1)  # slow down guessing
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    resp = JSONResponse({"ok": True, "user": req.username})
    resp.set_cookie(SESSION_COOKIE, _sign(req.username, int(time.time()) + SESSION_TTL_S), max_age=SESSION_TTL_S,
                    httponly=True, samesite="lax", secure=request.url.scheme == "https"
                    or request.headers.get("x-forwarded-proto") == "https")
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/api/health")
def health():
    return {"ok": True, "db": SETTINGS.db_path.exists(), "bank_id": SETTINGS.hindsight_bank_id,
            "llm": f"{SETTINGS.llm_provider}:{SETTINGS.llm_model}"}


def _agent(live_db: Path = UI_LIVE_DB):
    return agent_core.build_agent(SETTINGS, live_db_path=live_db)


def _guard(fn):
    """Run a model-backed request in a limited slot; map agent / provider failures to readable HTTP errors."""
    if not RUN_SLOTS.acquire(timeout=RUN_QUEUE_TIMEOUT_S):
        raise HTTPException(status_code=503, detail="The agent is busy with other requests - try again in a minute.")
    try:
        return fn()
    except (AgentError, LLMRefusal, LLMUnavailable) as ex:
        raise HTTPException(status_code=502, detail=f"{type(ex).__name__}: {ex}") from ex
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex
    finally:
        RUN_SLOTS.release()


class DecideRequest(BaseModel):
    report: str
    as_of: str | None = None
    rpo_id: str | None = None
    run_ids: list[str] | None = None
    rm_id: str | None = None
    writeback: bool = False


class QuestionRequest(BaseModel):
    question: str
    as_of: str | None = None


class DemoRequest(BaseModel):
    id: str


@app.get("/api/config")
def config():
    s = SETTINGS
    return {"llm_provider": s.llm_provider, "llm_model": s.llm_model, "llm_compact": s.llm_compact,
            "hindsight_base_url": s.hindsight_base_url, "bank_id": s.hindsight_bank_id,
            "default_as_of": s.default_as_of, "mental_models_in_reflect": s.mental_models_in_reflect}


@app.get("/api/scenarios")
def scenarios():
    out = []
    for sid, sc in load_holdout(SETTINGS).items():
        out.append({"id": sid, "day0": sc["day0"], "report": sc["day0_report"], "context": holdout_context(sc),
                    "supplier_id": sc["entities"]["supplier_id"], "rm_id": sc["entities"]["rm_id"],
                    "gold_best_action": sc["gold_best_action"], "trap": sc["trap"]})
    return out


@app.post("/api/decide")
def decide(req: DecideRequest):
    ctx = {k: v for k, v in {"rpo_id": req.rpo_id, "rm_id": req.rm_id, "run_ids": req.run_ids}.items() if v}
    return _guard(lambda: _agent().run(req.report, as_of=req.as_of or None, context=ctx,
                                       writeback=req.writeback).to_dict())


@app.post("/api/ask")
def ask_memory(req: QuestionRequest):
    return _guard(lambda: ask(_agent(), req.question, req.as_of or None).to_dict())


@app.post("/api/question")
def history_question(req: QuestionRequest):
    if not req.as_of:
        raise HTTPException(status_code=400, detail="as_of is required for history questions")
    return _guard(lambda: _agent().answer_question(req.question, req.as_of).to_dict())


@app.get("/api/demo/specs")
def demo_specs():
    return load_specs()


@app.post("/api/demo/run")
def demo_run(req: DemoRequest):
    spec = next((s for s in load_specs() if s["id"] == req.id), None)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"unknown demo scenario {req.id}")
    if spec["kind"] == "chain":
        DEMO_LIVE_DB.unlink(missing_ok=True)  # the closing-the-loop demo must start from an empty live DB
    result = _guard(lambda: run_spec(_agent(DEMO_LIVE_DB), spec, load_holdout(SETTINGS)))
    return {"spec": spec, "result": result, "checks": check_expectations(spec["expect"], result)}


@app.get("/api/docs/{doc_id}")
def document(doc_id: str, as_of: str):
    index = load_index(SETTINGS.corpus_path)
    doc = index.by_id.get(doc_id)
    if doc is None or not index.visible(doc, as_of):
        raise HTTPException(status_code=404, detail=f"{doc_id} is not a memory document visible as of {as_of}")
    return {"doc_id": doc.doc_id, "date": doc.date, "doc_type": doc.doc_type, "context": doc.context,
            "content": doc.content, "source_record_ids": list(doc.source_record_ids),
            "superseded_by": [{"doc_id": d.doc_id, "date": d.date} for d in index.superseding(doc_id, as_of)]}


@app.get("/api/results")
def results():
    def read(p: Path):
        return p.read_text() if p.exists() else None

    cards = sorted(p.stem for p in (ROOT / "eval" / "out" / "cards").glob("*.json"))
    score = ROOT / "eval" / "out" / "scorecard.json"
    return {"scorecard_md": read(ROOT / "eval" / "out" / "scorecard.md"),
            "scorecard": json.loads(score.read_text()) if score.exists() else None,
            "demo_report_md": read(ROOT / "demo" / "out" / "demo_report.md"), "cards": cards,
            "coverage": _coverage()}


def _coverage() -> dict:
    """What the prototype runs on: memory stored vs corpus, structured records, eval assets."""
    import sqlite3

    log = SETTINGS.retention_log_path
    stored = len({r["doc_id"] for r in map(json.loads, log.read_text().splitlines()) if r.get("status") == "ok"}) \
        if log.exists() else 0
    splits = [json.loads(line)["split"] for line in SETTINGS.corpus_path.open()]
    con = sqlite3.connect(f"file:{SETTINGS.db_path}?mode=ro", uri=True)
    try:
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("disruption_events", "decisions", "commitments", "negotiations", "rm_purchase_orders")}
    finally:
        con.close()
    return {"memory_docs_stored": stored, "memory_docs_total": splits.count("memory"),
            "holdout_docs": splits.count("holdout"), "eval_questions": sum(1 for _ in (SETTINGS.dataset_dir /
                                                                                         "eval_questions.jsonl").open()),
            "holdout_scenarios": len(load_holdout(SETTINGS)), "planted_patterns": 12, **counts}


@app.get("/api/results/cards/{sid}")
def result_card(sid: str):
    p = ROOT / "eval" / "out" / "cards" / f"{sid}.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"no saved card for {sid}")
    return json.loads(p.read_text())


@app.post("/api/live/reset")
def reset_live():
    UI_LIVE_DB.unlink(missing_ok=True)
    return {"ok": True, "message": "UI live DB cleared (Hindsight retains are not deleted)"}


if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        target = WEB_DIST / path
        return FileResponse(target if path and target.is_file() else WEB_DIST / "index.html")
