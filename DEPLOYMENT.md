# Deploying ClipForge AI on Render (Docker)

## 1. Push to GitHub
```bash
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```
`render.yaml` must be at the repository root (it is).

## 2. Create the service
1. Render dashboard → **New → Blueprint** → select the repository.
2. **Review the plan and disk before applying.** The blueprint requests a `standard` instance and a 20 GB persistent disk at `/data` because video rendering needs RAM/CPU and the SQLite database + clips must survive restarts. These are paid resources billed to your Render account — check current pricing, and change `plan`/`sizeGB` in `render.yaml` if you prefer.
3. `SECRET_KEY` is generated automatically. **Never change it afterwards** — saved user API keys are encrypted with it and would become unreadable.
4. Optional: set `INVITE_CODE` (dashboard → Environment) to restrict sign-ups, or `ALLOW_REGISTRATION=0` after creating your account.
5. Apply. First build takes several minutes (FFmpeg + fonts).

## 3. Verify
- `https://<service>.onrender.com/health` → `{"status":"ok",...}`
- Register, open **Studio Settings**, paste an OpenAI or Gemini key → models appear in the dropdowns.
- Upload a short (1–3 min) video with clear speech; watch progress; play and download the clips.

## 4. Operations
- Logs: Render dashboard → Logs. Failed jobs show a user-safe reason in the UI and a detailed error in the logs.
- Clips and temp files are deleted after `RETENTION_HOURS` (default 72).
- Keep `--workers 1` (queue workers run inside the process). Raise `WORKER_THREADS` only if the instance has spare CPU/RAM.
- Back up `/data/clipforge.db` if account data matters (Render disk snapshots).
