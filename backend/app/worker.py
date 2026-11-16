"""Durable job worker: claims jobs from SQLite, runs the full pipeline, records clips, cleans up."""
import logging
import re
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

from . import analysis, db, providers, video
from .config import get_settings
from .security import UnsafeURL, check_public_https_url, decrypt_secret

log = logging.getLogger("clipforge.worker")
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
STOP = threading.Event()


class JobFailed(Exception):
    """A failure whose message is safe to show to the user."""


def download_url(url: str, dest_dir: Path, jid: str, max_bytes: int, cancel) -> Path:
    cur = url
    for _ in range(4):
        check_public_https_url(cur)  # re-validated on every redirect hop
        r = requests.get(cur, stream=True, timeout=(10, 60), allow_redirects=False, headers={"User-Agent": "ClipForge/2.0"})
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            cur = urljoin(cur, r.headers["location"]); r.close(); continue
        break
    else:
        raise JobFailed("Too many redirects.")
    if not r.ok:
        raise JobFailed(f"The video URL returned HTTP {r.status_code}.")
    ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if not (ct.startswith("video/") or ct in ("application/octet-stream", "binary/octet-stream", "application/mp4")):
        raise JobFailed("That URL is not a direct video file (platform page links like YouTube/Facebook are not supported; upload the file instead).")
    declared = int(r.headers.get("content-length") or 0)
    if declared > max_bytes:
        raise JobFailed("The video at that URL is larger than the maximum allowed size.")
    dest = dest_dir / f"{jid}_source.mp4"
    total = 0
    with dest.open("wb") as f:
        for chunk in r.iter_content(1024 * 1024):
            if cancel(): raise video.Cancelled()
            total += len(chunk)
            if total > max_bytes:
                f.close(); dest.unlink(missing_ok=True); raise JobFailed("The video at that URL is larger than the maximum allowed size.")
            f.write(chunk)
    return dest


def _set(jid, status, msg, progress):
    db.update_job(jid, status=status, message=msg, progress=progress)


def process(job) -> None:
    s = get_settings()
    jid, uid = job["id"], job["user_id"]
    opts = __import__("json").loads(job["options_json"])
    cancel = lambda: db.is_cancelled(jid)
    work = s.uploads_dir / jid
    work.mkdir(parents=True, exist_ok=True)
    src = Path(job["source_path"]) if job["source_path"] else None
    try:
        row = db.get_api_key(uid, opts["provider"])
        if not row:
            raise JobFailed(f"No {opts['provider']} API key saved. Add one in Settings → API keys.")
        key = decrypt_secret(row["secret_enc"])
        t_model, a_model = opts.get("transcription_model"), opts.get("analysis_model")
        if not t_model or not a_model:
            raise JobFailed("Select transcription and analysis models in Studio Settings.")

        if src is None:
            _set(jid, "downloading", "Downloading video…", 5)
            src = download_url(opts["video_url"], work, jid, s.max_upload_bytes, cancel)
            db.update_job(jid, source_path=str(src))
        info = video.probe(src)
        if info["duration"] < opts["min_duration"] + 1:
            raise JobFailed(f"The video is too short ({info['duration']:.0f}s) for {opts['min_duration']}s clips.")
        if info["duration"] > s.max_source_seconds:
            raise JobFailed(f"Videos longer than {s.max_source_seconds // 60} minutes are not supported on this server.")
        if not info["has_audio"]:
            raise JobFailed("The video has no audio track, so speech-based clip selection is not possible.")

        _set(jid, "transcribing", "Extracting audio…", 10)
        chunks = video.extract_audio_chunks(src, work, info["duration"], 600, cancel)
        results = []
        for i, ch in enumerate(chunks):
            _set(jid, "transcribing", f"Transcribing audio ({i + 1}/{len(chunks)})…", 12 + int(26 * i / max(1, len(chunks))))
            results.append(providers.transcribe(opts["provider"], key, t_model, ch["path"], opts["caption_language"], 600))
            if cancel(): raise video.Cancelled()
        tr = analysis.merge_chunks(results, [c["offset"] for c in chunks])
        if len(tr["words"]) < 15:
            raise JobFailed("Not enough speech was detected to find clips.")

        _set(jid, "analyzing", "Analyzing scenes and audio energy…", 40)
        scenes = video.scene_times(src, info["duration"], cancel)
        loud = video.loudness_profile(src, info["duration"], 1.0, cancel)
        cands = analysis.build_candidates(tr["segments"], info["duration"], scenes, loud, opts["min_duration"], opts["max_duration"])
        if not cands:
            raise JobFailed("Could not build clip candidates. Try a wider duration range or a video with continuous speech.")

        _set(jid, "selecting", f"AI is scoring {len(cands)} moments for comedy, engagement and importance…", 50)
        use_ai, note = True, ""
        try:
            analysis.llm_score(cands, opts["provider"], key, a_model, opts["caption_language"])
        except providers.ProviderError as e:
            if "rejected the API key" in str(e): raise JobFailed(str(e))
            use_ai, note = False, f" (AI scoring unavailable: {str(e)[:120]}; used audio/transcript heuristics)"
            log.warning("AI scoring failed for job %s: %s", jid, e)
        picks = analysis.select_clips(cands, opts["categories"], use_ai)
        if not picks:
            raise JobFailed("No suitable clips found.")

        for n, p in enumerate(picks):
            if cancel(): raise video.Cancelled()
            _set(jid, "rendering", f"Rendering clip {n + 1} of {len(picks)} ({p['label']})…", 55 + int(40 * n / len(picks)))
            fname = f"{jid}_{p['category']}.mp4"
            ro = {"format": opts["format"], "quality": opts["quality"], "framing": opts["framing"], "caption_style": opts["caption_style"], "audio": opts["audio"]}
            notes = video.render_clip(src, info, p["start"], p["end"], s.outputs_dir / fname, ro, tr["words"], work, cancel,
                                      progress=lambda m, n=n, p=p: _set(jid, "rendering", f"Clip {n + 1}/{len(picks)}: {m}", 55 + int(40 * n / len(picks))))
            notes["selection"] = p["method"]
            db.add_clip(jid, n, p["label"], p["title"], p["reason"], p["score"], round(p["start"], 2), round(p["end"], 2), fname, notes)
        _set(jid, "completed", "Your clips are ready." + note, 100)
    except video.Cancelled:
        db.update_job(jid, status="cancelled", message="Cancelled.", progress=0)
    except JobFailed as e:
        db.update_job(jid, status="failed", message="Failed.", error=str(e)[:500])
    except UnsafeURL as e:
        db.update_job(jid, status="failed", message="Failed.", error=str(e))
    except providers.ProviderError as e:
        db.update_job(jid, status="failed", message="Failed.", error=str(e)[:500])
    except video.FFmpegError as e:
        log.error("ffmpeg failure job %s: %s", jid, e)
        db.update_job(jid, status="failed", message="Failed.", error="Video processing failed. The file may be corrupted or use an unsupported codec.")
    except Exception:
        log.exception("Unhandled error in job %s", jid)
        db.update_job(jid, status="failed", message="Failed.", error="Unexpected server error while processing this video.")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if src and src.exists():
            try: src.unlink()
            except OSError: pass


def worker_loop(idx: int) -> None:
    while not STOP.is_set():
        try:
            job = db.claim_next_job()
        except Exception:
            log.exception("claim failed"); time.sleep(3); continue
        if job is None:
            STOP.wait(1.5); continue
        log.info("worker %d processing job %s", idx, job["id"])
        process(job)


def cleanup_loop() -> None:
    while not STOP.is_set():
        try:
            s = get_settings()
            for j in db.expired_jobs(time.time() - s.retention_hours * 3600):
                for c in db.job_clips(j["id"]):
                    (s.outputs_dir / c["filename"]).unlink(missing_ok=True)
                shutil.rmtree(s.uploads_dir / j["id"], ignore_errors=True)
                db.delete_job(j["id"])
        except Exception:
            log.exception("cleanup failed")
        STOP.wait(1800)


def start_workers() -> None:
    STOP.clear()
    n = db.requeue_interrupted()
    if n: log.warning("re-queued %d interrupted job(s)", n)
    for i in range(get_settings().worker_threads):
        threading.Thread(target=worker_loop, args=(i,), daemon=True, name=f"worker-{i}").start()
    threading.Thread(target=cleanup_loop, daemon=True, name="cleanup").start()
