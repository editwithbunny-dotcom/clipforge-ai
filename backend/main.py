"""ClipForge AI — FastAPI application (auth, API keys, settings, jobs, secure downloads, static frontend)."""
import json
import logging
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import db, options as opt, providers, security, video, worker
from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("clipforge")
DUMMY_HASH = security.hash_password("clipforge-dummy-password")
COOKIE = "cf_session"
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    db.init_db()
    worker.start_workers()
    log.info("ClipForge started (env=%s, data=%s)", s.env, s.data_dir)
    yield
    worker.STOP.set()


app = FastAPI(title="ClipForge AI", version="2.0.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ------------------------------------------------------------------ security middleware
@app.middleware("http")
async def security_layer(request: Request, call_next):
    s = get_settings()
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # CSRF defence in depth: same-origin Origin header + custom header (cannot be set cross-site without CORS preflight)
        origin = request.headers.get("origin")
        if origin:
            host = urlparse(origin).netloc
            allowed = {request.headers.get("host", "")} | {urlparse(o).netloc for o in s.allowed_origins}
            if host not in allowed:
                return JSONResponse({"detail": "Cross-origin request blocked."}, status_code=403)
        if request.headers.get("x-requested-with") != "ClipForge":
            return JSONResponse({"detail": "Missing required request header."}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; media-src 'self' blob:; style-src 'self'; script-src 'self'; "
                                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    if s.cookie_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def current_user(request: Request):
    tok = request.cookies.get(COOKIE)
    uid = security.read_token(tok) if tok else None
    user = db.get_user(uid) if uid else None
    if not user:
        raise HTTPException(401, "Please sign in.")
    return user


def set_session(resp: Response, uid: int) -> None:
    s = get_settings()
    resp.set_cookie(COOKIE, security.create_token(uid), max_age=s.session_hours * 3600, httponly=True, secure=s.cookie_secure, samesite="lax", path="/")


# ------------------------------------------------------------------ auth
class Creds(BaseModel):
    email: str
    password: str
    invite_code: Optional[str] = ""


@app.post("/api/auth/register")
def register(c: Creds, request: Request, response: Response):
    s = get_settings()
    if not s.allow_registration:
        raise HTTPException(403, "Registration is closed.")
    if s.invite_code and c.invite_code != s.invite_code:
        raise HTTPException(403, "A valid invite code is required.")
    if not security.limiter.allow(f"reg:{client_ip(request)}", 20, 3600):
        raise HTTPException(429, "Too many sign-ups from this address. Try again later.")
    try:
        security.validate_credentials(c.email, c.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    uid = db.create_user(c.email, security.hash_password(c.password))
    if uid is None:
        raise HTTPException(409, "An account with that email already exists.")
    set_session(response, uid)
    return {"id": uid, "email": c.email.lower()}


@app.post("/api/auth/login")
def login(c: Creds, request: Request, response: Response):
    if not (security.limiter.allow(f"login:{client_ip(request)}", 20, 600) and security.limiter.allow(f"login:{c.email.lower()}", 8, 600)):
        raise HTTPException(429, "Too many sign-in attempts. Wait a few minutes.")
    u = db.get_user_by_email(c.email)
    # always run one scrypt verification so response time does not reveal whether the email exists
    ok = security.verify_password(c.password, u["password_hash"] if u else DUMMY_HASH)
    if not u or not ok:
        raise HTTPException(401, "Incorrect email or password.")
    set_session(response, u["id"])
    return {"id": u["id"], "email": u["email"]}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return {"id": user["id"], "email": user["email"], "settings": {**opt.DEFAULTS, **db.user_settings(user)},
            "keys": [{"provider": k["provider"], "hint": k["hint"]} for k in db.list_api_keys(user["id"])]}


# ------------------------------------------------------------------ API keys & models
class KeyIn(BaseModel):
    key: str


@app.put("/api/keys/{provider}")
async def put_key(provider: str, body: KeyIn, request: Request, user=Depends(current_user)):
    if provider not in providers.PROVIDERS:
        raise HTTPException(400, "Unknown provider.")
    key = body.key.strip()
    if not (10 <= len(key) <= 300) or re.search(r"\s", key):
        raise HTTPException(400, "That does not look like a valid API key.")
    if not security.limiter.allow(f"key:{user['id']}", 10, 600):
        raise HTTPException(429, "Too many attempts. Wait a few minutes.")
    try:
        models = await run_in_threadpool(providers.list_models, provider, key)  # proves the key works before saving it
    except providers.ProviderError as e:
        raise HTTPException(400, str(e))
    db.set_api_key(user["id"], provider, security.encrypt_secret(key), security.mask_secret(key))
    return {"provider": provider, "hint": security.mask_secret(key), "models": models}


@app.delete("/api/keys/{provider}")
def del_key(provider: str, user=Depends(current_user)):
    db.delete_api_key(user["id"], provider)
    return {"ok": True}


@app.get("/api/models/{provider}")
async def models(provider: str, user=Depends(current_user)):
    row = db.get_api_key(user["id"], provider) if provider in providers.PROVIDERS else None
    if not row:
        raise HTTPException(404, "Save an API key for this provider first.")
    try:
        key = security.decrypt_secret(row["secret_enc"])
        m = await run_in_threadpool(providers.list_models, provider, key)
    except (providers.ProviderError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    m["defaults"] = {"transcription": providers.prefer(m["transcription"], [r"^whisper-1$", r"whisper", r"flash", r"."]),
                     "analysis": providers.prefer(m["analysis"], [r"^gpt-4\.1-mini$", r"^gpt-4o-mini$", r"mini", r"flash", r"."])}
    return m


# ------------------------------------------------------------------ studio settings
@app.put("/api/settings")
def put_settings(body: dict, user=Depends(current_user)):
    try:
        clean = opt.validate_options(body)
    except opt.OptionsError as e:
        raise HTTPException(400, str(e))
    db.save_user_settings(user["id"], clean)
    return clean


# ------------------------------------------------------------------ jobs
def job_json(j) -> dict:
    d = {"id": j["id"], "status": j["status"], "message": j["message"], "progress": j["progress"], "error": j["error"],
         "source_name": j["source_name"], "created_at": j["created_at"], "clips": []}
    for c in db.job_clips(j["id"]):
        meta = json.loads(c["meta_json"] or "{}")
        d["clips"].append({"category": c["category"], "title": c["title"], "reason": c["reason"], "score": c["score"], "start": c["start_s"], "end": c["end_s"],
                           "duration": round(c["end_s"] - c["start_s"], 1), "url": f"/api/clips/{c['filename']}", "meta": meta})
    return d


@app.post("/api/jobs")
async def create_job(request: Request, file: Optional[UploadFile] = File(None), video_url: Optional[str] = Form(None), options: str = Form("{}"), user=Depends(current_user)):
    s = get_settings()
    uid = user["id"]
    try:  # reject oversize uploads before the multipart body is spooled to disk
        if int(request.headers.get("content-length") or 0) > s.max_upload_bytes + 1024 * 1024:
            raise HTTPException(413, f"File is larger than {s.max_upload_bytes // 1024 ** 2} MB.")
    except ValueError:
        raise HTTPException(400, "Invalid Content-Length.")
    try:
        o = opt.validate_options(json.loads(options))
    except (opt.OptionsError, json.JSONDecodeError) as e:
        raise HTTPException(400, str(e) if isinstance(e, opt.OptionsError) else "Invalid options.")
    if not o["transcription_model"] or not o["analysis_model"]:
        raise HTTPException(400, "Choose a transcription model and an analysis model in Studio Settings.")
    if not db.get_api_key(uid, o["provider"]):
        raise HTTPException(400, f"Save your {o['provider']} API key first (Settings → API keys).")
    if (file is None) == (not video_url):
        raise HTTPException(400, "Provide exactly one video file or one video URL.")
    if db.count_active_jobs(uid) >= s.max_active_jobs_per_user:
        raise HTTPException(429, f"You already have {s.max_active_jobs_per_user} jobs running. Wait for one to finish or cancel it.")
    if db.count_jobs_since(uid, time.time() - 86400) >= s.max_jobs_per_day:
        raise HTTPException(429, "Daily job limit reached. Try again tomorrow.")
    if shutil.disk_usage(s.data_dir).free < 100 * 1024 ** 2:
        raise HTTPException(503, "The server is low on disk space. Try again later.")

    src_path, name = None, "video-url"
    if file is not None:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in VIDEO_EXT:
            raise HTTPException(400, "Unsupported file type. Use MP4, MOV, MKV, WEBM or M4V.")
        name = re.sub(r"[^\w.\- ]", "_", file.filename or "upload")[:120]
        dest = s.uploads_dir / f"{uuid.uuid4().hex}_source{ext}"
        size = 0
        try:
            with dest.open("wb") as f:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > s.max_upload_bytes:
                        raise HTTPException(413, f"File is larger than {s.max_upload_bytes // 1024 ** 2} MB.")
                    f.write(chunk)
            await run_in_threadpool(video.probe, dest)  # rejects non-video / corrupt files before queueing
        except HTTPException:
            dest.unlink(missing_ok=True); raise
        except Exception:
            dest.unlink(missing_ok=True)
            raise HTTPException(400, "That file could not be read as a video.")
        src_path = str(dest)
    else:
        try:
            await run_in_threadpool(security.check_public_https_url, video_url.strip())
        except security.UnsafeURL as e:
            raise HTTPException(400, str(e))
        o["video_url"] = video_url.strip()
        name = urlparse(video_url).hostname or "video-url"
    jid = db.create_job(uid, src_path, name, o)
    return {"job_id": jid}


@app.get("/api/jobs")
def jobs(user=Depends(current_user)):
    return [job_json(j) for j in db.list_jobs(user["id"])]


@app.get("/api/jobs/{job_id}")
def job(job_id: str, user=Depends(current_user)):
    j = db.get_job(job_id, user["id"])
    if not j:
        raise HTTPException(404, "Job not found.")
    return job_json(j)


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str, user=Depends(current_user)):
    if not db.request_cancel(job_id, user["id"]):
        raise HTTPException(409, "Job is not running.")
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, user=Depends(current_user)):
    s = get_settings()
    j = db.get_job(job_id, user["id"])
    if not j:
        raise HTTPException(404, "Job not found.")
    if j["status"] in db.ACTIVE:
        raise HTTPException(409, "Cancel the job before deleting it.")
    for c in db.job_clips(job_id):
        (s.outputs_dir / c["filename"]).unlink(missing_ok=True)
    db.delete_job(job_id)
    return {"ok": True}


@app.get("/api/clips/{filename}")
def clip(filename: str, download: int = 0, user=Depends(current_user)):
    if not re.fullmatch(r"[a-f0-9]{32}_(comedy|engagement|important)\.mp4", filename):
        raise HTTPException(400, "Invalid filename.")
    owner = db.clip_owner(filename)
    p = get_settings().outputs_dir / filename
    if not owner or owner["user_id"] != user["id"] or not p.exists():
        raise HTTPException(404, "Clip not found.")  # same response for foreign clips: no existence leak
    return FileResponse(p, media_type="video/mp4", filename=filename if download else None, content_disposition_type="attachment" if download else "inline")


@app.get("/health")
def health():
    try:
        db.conn().execute("SELECT 1").fetchone()
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok", "service": "ClipForge AI"}


# ------------------------------------------------------------------ frontend
_fe = get_settings().frontend_dir
app.mount("/static", StaticFiles(directory=str(_fe / "static")), name="static")


@app.get("/")
def home():
    return FileResponse(_fe / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})
