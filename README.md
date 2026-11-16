# ClipForge AI

Turn long videos into captioned, face-tracked short clips. Upload a video (or give a direct HTTPS link to one), pick an AI provider (OpenAI or Google Gemini) and models, and ClipForge finds the **Comedy**, **Engagement** and **Important Moment** clips and renders them as 9:16 (or 1:1 / 16:9) MP4s.

## What it does

| Area | Implementation |
|---|---|
| Accounts | Email + password (scrypt), HttpOnly SameSite session cookie, login/sign-up rate limits, optional invite code |
| API keys | Each user's OpenAI/Gemini key is verified against the provider, then stored **encrypted** (Fernet, derived from `SECRET_KEY`). Never returned to the browser, never put in URLs or logs |
| Models | Dropdowns are filled from the provider's live models endpoint using the user's key — no hardcoded model names |
| Transcription | OpenAI Whisper models (word + segment timestamps) or any Gemini model; audio is chunked so long videos work |
| Clip selection | Candidate windows cut on sentence boundaries → scored 0–10 for comedy / engagement / importance by the chosen analysis model, combined with audio-energy, laughter cues and scene changes → exact best non-overlapping assignment, one clip per category. If AI scoring fails the job falls back to heuristics and says so |
| Rendering | Face-tracking smart crop (smoothed, dead-band, speed-limited), center crop, or blurred-background fit; 720p/1080p; 9:16, 1:1, 16:9; burned-in ASS captions (3-word karaoke highlight or clean); audio denoise/compress/EQ/loudness-normalize; H.264 + AAC |
| Studio Settings | Every control is saved per user and sent with the job: provider, models, categories, clip length, format, resolution, framing, captions, language, audio |
| Queue | SQLite-backed durable queue with worker thread(s); jobs survive restarts (re-queued once), cancellable, progress reported |
| Security | CSP (no inline scripts), CSRF origin + custom-header check, ownership-checked downloads, SSRF protection (HTTPS only, public IPs only, redirects re-validated), upload size/type checks, per-user concurrency + daily quotas, retention cleanup |

## Known limitations (read before launch)

- **Face tracking is frontal-face only** (OpenCV LBP cascade via scikit-image). It does not do active-speaker detection; with several people it follows the most consistent/largest face. Profile views and small/dark faces may fall back to center crop (the clip's metadata says which was used).
- **Selection is text- and audio-based.** The AI sees transcript windows, not video frames, so purely visual comedy is invisible to it.
- **Gemini transcript timestamps are model-generated** and can drift by a second or two; OpenAI Whisper timestamps are more reliable. Always check caption timing on a test clip.
- **URL ingestion is direct media files only.** YouTube/Facebook/Instagram page links are not downloaded (platform terms; use uploads or authorized exports).
- **Single-server architecture.** SQLite + in-process workers + local disk. That is appropriate for one Render instance with a persistent disk, not for horizontal scaling. Moving to Postgres + Redis + object storage is the next step if you outgrow it.
- Rendering is CPU-bound: expect roughly real-time to several × real-time per clip depending on instance size.
- The URL downloader validates DNS before connecting; a DNS-rebinding race remains theoretically possible (mitigated by blocking redirects to private IPs and by running as a non-root user).
- Billing/payments are not implemented. Per-user quotas are the only usage control.

## Run locally

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt        # also needs ffmpeg + ffprobe on PATH
export CLIPFORGE_ENV=development SECRET_KEY=dev-only-change-me-dev-only-change-me
uvicorn main:app --reload --port 10000
# open http://localhost:10000
```

Tests:
```bash
cd backend
python tests/test_core.py          # auth, encryption, SSRF, queue
python tests/test_pipeline.py      # real FFmpeg + face-tracking pipeline against a local fake OpenAI/Gemini server
python -m unittest tests.test_api  # HTTP layer (needs requirements-dev.txt)
```

## Docker

```bash
SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))") docker compose up --build
```

## Configuration (environment variables)

`SECRET_KEY` (required, 32+ chars) · `DATA_DIR` (default `/data`) · `COOKIE_SECURE` (1 behind HTTPS) · `ALLOW_REGISTRATION` · `INVITE_CODE` · `MAX_UPLOAD_MB` (1024) · `MAX_SOURCE_MINUTES` (120) · `MAX_ACTIVE_JOBS_PER_USER` (2) · `MAX_JOBS_PER_DAY` (20) · `WORKER_THREADS` (1) · `RETENTION_HOURS` (72) · `FRONTEND_ORIGIN` (extra allowed origins, optional).

See `DEPLOYMENT.md` for Render.
