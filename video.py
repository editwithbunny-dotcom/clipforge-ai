"""Video engine: probing, audio extraction, scene detection, face tracking, smart crop, subtitles, rendering."""
import json
import math
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

ASSETS = Path(__file__).resolve().parent / "assets"


class Cancelled(Exception):
    pass


class FFmpegError(RuntimeError):
    pass


def _kill(p) -> None:
    p.kill(); p.wait()
    for f in (p.stdout, p.stderr):
        try: f.close()
        except Exception: pass


def run(cmd: List[str], timeout: Optional[int] = None, cancel: Optional[Callable[[], bool]] = None, binary_out=False):
    """Run a subprocess, polling for cancellation. Raises FFmpegError with the tail of stderr on failure."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = [], []
    t_out = threading.Thread(target=lambda: out.append(p.stdout.read()), daemon=True)
    t_err = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
    t_out.start(); t_err.start()
    start = time.monotonic()
    while p.poll() is None:
        if cancel and cancel():
            _kill(p); raise Cancelled()
        if timeout and time.monotonic() - start > timeout:
            _kill(p); raise FFmpegError(f"Timed out after {timeout}s: {cmd[0]}")
        time.sleep(0.2)
    t_out.join(); t_err.join()
    p.stdout.close(); p.stderr.close()
    stdout = out[0] if out else b""
    stderr = (err[0] if err else b"").decode("utf-8", "replace")
    if p.returncode != 0:
        raise FFmpegError(stderr[-2500:] or f"{cmd[0]} exited with {p.returncode}")
    return stdout if binary_out else stdout.decode("utf-8", "replace")


# ------------------------------------------------------------------ probing
def probe(path) -> dict:
    data = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]))
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        raise ValueError("The file has no video stream.")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"])) % 360
    if str(v.get("tags", {}).get("rotate", "")).lstrip("-").isdigit():
        rot = int(v["tags"]["rotate"]) % 360
    if rot in (90, 270):
        w, h = h, w
    num, _, den = (v.get("avg_frame_rate") or "30/1").partition("/")
    try:
        fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    except ValueError:
        fps = 30.0
    dur = float(data["format"].get("duration") or v.get("duration") or 0)
    return {"duration": dur, "width": w, "height": h, "fps": fps if 1 <= fps <= 120 else 30.0, "has_audio": a is not None}


# ------------------------------------------------------------------ audio for transcription
def extract_audio_chunks(src, workdir: Path, duration: float, chunk_seconds=600, cancel=None) -> List[dict]:
    """Mono 16 kHz MP3 chunks (small enough for provider upload limits). Returns [{path, offset}]."""
    chunks, t, i = [], 0.0, 0
    while t < duration - 0.5:
        dst = workdir / f"audio_{i:03d}.mp3"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-t", f"{chunk_seconds}", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "libmp3lame", "-b:a", "48k", str(dst)], timeout=900, cancel=cancel)
        if dst.exists() and dst.stat().st_size > 2000:
            chunks.append({"path": dst, "offset": t})
        t += chunk_seconds; i += 1
    return chunks


# ------------------------------------------------------------------ scene changes & loudness
def scene_times(src, duration: float, cancel=None) -> List[float]:
    try:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(src), "-vf", "fps=4,scale=320:-2,select='gt(scene,0.3)',showinfo", "-an", "-f", "null", "-"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900)
        vals = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", p.stdout)]
        return sorted({round(v, 2) for v in vals if 1 < v < duration - 1})
    except Exception:
        return []


def loudness_profile(src, duration: float, step=1.0, cancel=None) -> List[float]:
    """Per-second RMS energy (0..1) used as a laughter/excitement proxy."""
    try:
        raw = run(["ffmpeg", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], timeout=900, cancel=cancel, binary_out=True)
    except FFmpegError:
        return []
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    n = int(8000 * step)
    if len(a) < n:
        return []
    frames = a[: len(a) // n * n].reshape(-1, n)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    return (rms / (rms.max() + 1e-9)).round(4).tolist()


# ------------------------------------------------------------------ face tracking
_cascade = None


def _get_cascade():
    global _cascade
    if _cascade is None:
        from skimage.feature import Cascade
        _cascade = Cascade(str(ASSETS / "lbpcascade_frontalface_opencv.xml"))
    return _cascade


def detect_faces(gray: np.ndarray) -> List[tuple]:
    """gray: float32 (H,W) in 0..1. Returns [(cx, cy, size)] in pixels, merged and de-duplicated."""
    h, w = gray.shape
    dets = _get_cascade().detect_multi_scale(img=gray, scale_factor=1.2, step_ratio=1, min_size=(int(w * 0.06),) * 2, max_size=(int(w * 0.8),) * 2)
    boxes = [(d["c"] + d["width"] / 2, d["r"] + d["height"] / 2, (d["width"] + d["height"]) / 2) for d in dets]
    merged = []
    for b in sorted(boxes, key=lambda x: -x[2]):
        if all(math.hypot(b[0] - m[0], b[1] - m[1]) > 0.6 * m[2] for m in merged):
            merged.append(b)
    return merged


def sample_faces(src, start: float, duration: float, src_w: int, src_h: int, fps=2.0, width=360, cancel=None) -> List[dict]:
    """Sample frames at `fps` and return [{t, faces:[(cx,cy,size) normalized 0..1]}]."""
    h = int(round(width * src_h / src_w / 2) * 2)
    raw = run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src), "-vf", f"fps={fps},scale={width}:{h},format=gray",
               "-f", "rawvideo", "-"], timeout=900, cancel=cancel, binary_out=True)
    fsize = width * h
    n = len(raw) // fsize
    out = []
    for i in range(n):
        g = np.frombuffer(raw, dtype=np.uint8, count=fsize, offset=i * fsize).reshape(h, width).astype(np.float32) / 255.0
        faces = [(cx / width, cy / h, s / width) for cx, cy, s in detect_faces(g)]
        out.append({"t": i / fps, "faces": faces})
        if cancel and i % 10 == 0 and cancel():
            raise Cancelled()
    return out


def build_track(samples: List[dict]) -> List[Optional[tuple]]:
    """Pick one subject per sample with temporal consistency: prefer the face nearest the previous subject,
    reject isolated one-frame detections (typical false positives), and return [(cx, cy) or None]."""
    picks: List[Optional[tuple]] = []
    prev = None
    for s in samples:
        faces = s["faces"]
        if not faces:
            picks.append(None); continue
        if prev is None:
            best = max(faces, key=lambda f: f[2])
        else:
            best = min(faces, key=lambda f: math.hypot(f[0] - prev[0], f[1] - prev[1]) - 0.5 * f[2])
            if math.hypot(best[0] - prev[0], best[1] - prev[1]) > 0.35:  # big jump: only accept if clearly larger than the previous face
                big = max(faces, key=lambda f: f[2])
                best = big if big[2] > 1.3 * prev[2] else best
        picks.append((best[0], best[1], best[2]))
        prev = best
    # drop isolated detections surrounded by misses
    for i, p in enumerate(picks):
        if p is not None:
            nb = [picks[j] for j in (i - 1, i + 1) if 0 <= j < len(picks)]
            if nb and all(x is None for x in nb) and len(picks) > 4:
                picks[i] = None
    return [(p[0], p[1]) if p else None for p in picks]


def plan_axis_path(track: List[Optional[tuple]], axis: int, fps_samples: float, duration: float, out_fps: float,
                   crop_frac: float, deadband=0.06, smooth_s=0.8, max_speed=0.5) -> Optional[List[float]]:
    """Turn face positions into a smooth per-output-frame crop-start in 0..(1-crop_frac) (fraction of the source axis).
    Holds still inside a dead-band, then glides. Returns None if too few detections to trust tracking."""
    pts = [(i / fps_samples, p[axis]) for i, p in enumerate(track) if p is not None]
    if len(pts) < max(3, 0.2 * len(track)):
        return None
    ts = np.array([p[0] for p in pts]); vs = np.array([p[1] for p in pts])
    n = max(1, int(duration * out_fps))
    t_out = np.arange(n) / out_fps
    center = np.interp(t_out, ts, vs)  # holds first/last value outside the detected range
    k = max(1, int(smooth_s * out_fps))
    pad = np.pad(center, (k, k), mode="edge")
    center = np.convolve(pad, np.ones(2 * k + 1) / (2 * k + 1), mode="valid")[:n]
    # dead-band follow: camera only moves when the subject leaves the central zone; speed-limited
    cam = np.empty(n); cur = center[0]; step = max_speed / out_fps
    for i in range(n):
        d = center[i] - cur
        if abs(d) > deadband * crop_frac:
            d -= math.copysign(deadband * crop_frac, d)
            cur += max(-step, min(step, d * 0.15))
        cam[i] = cur
    half = crop_frac / 2
    return np.clip(cam - half, 0, 1 - crop_frac).tolist()


# ------------------------------------------------------------------ subtitles (ASS)
def _ass_time(t: float) -> str:
    cs = int(round(max(t, 0) * 100)); h, cs = divmod(cs, 360000); m, cs = divmod(cs, 6000); s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(t: str) -> str:
    return t.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def make_ass(words: List[dict], clip_start: float, clip_end: float, width: int, height: int, style: str, path: Path) -> int:
    """words: [{start,end,text}] (absolute seconds). style: bold (karaoke highlight, 3 words) | clean (up to 7 words). Returns caption count."""
    ws = [w for w in words if w["end"] > clip_start and w["start"] < clip_end and w["text"].strip()]
    per = 3 if style == "bold" else 7
    fs = int(height * (0.052 if style == "bold" else 0.036)) if height >= width else int(height * 0.06)
    margin_v = int(height * (0.2 if height > width else 0.08))
    outline = max(2, round(fs * (0.09 if style == "bold" else 0.05)))
    hdr = ("[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
           "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
           "Style: Main,Noto Sans,%d,%s,%s,&H00000000,&H64000000,%d,0,0,0,100,100,0,0,1,%d,%d,2,%d,%d,%d,1\n\n[Events]\n"
           "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n") % (
        width, height, fs, "&H0000E5FF" if style == "bold" else "&H00FFFFFF", "&H00FFFFFF", 1 if style == "bold" else 0,
        outline, 1 if style == "bold" else 0, int(width * 0.06), int(width * 0.06), margin_v)
    lines, count = [], 0
    for i in range(0, len(ws), per):
        grp = ws[i:i + per]
        a = max(grp[0]["start"], clip_start) - clip_start
        b = min(max(grp[-1]["end"], grp[0]["start"] + 0.3), clip_end) - clip_start
        if i + per < len(ws):  # don't linger into the next caption
            b = min(b, max(ws[i + per]["start"] - clip_start, a + 0.25))
        if b - a < 0.15:
            continue
        if style == "bold":
            parts = []
            for w in grp:
                cs = max(1, int(round((min(w["end"], clip_end) - max(w["start"], clip_start)) * 100)))
                parts.append(f"{{\\k{cs}}}{_esc(w['text']).upper()}")
            text = " ".join(parts)
        else:
            text = " ".join(_esc(w["text"]) for w in grp)
        lines.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Main,,0,0,0,,{text}")
        count += 1
    path.write_text(hdr + "\n".join(lines) + "\n", encoding="utf-8")
    return count


# ------------------------------------------------------------------ rendering
AUDIO_CHAINS = {
    "off": "",
    "clean": "highpass=f=70,afftdn=nr=12:nf=-30,loudnorm=I=-16:TP=-1.5:LRA=11",
    "studio": "highpass=f=80,afftdn=nr=18:nf=-28,acompressor=threshold=-21dB:ratio=3:attack=15:release=250:makeup=3,equalizer=f=3000:width_type=q:w=1.2:g=2,loudnorm=I=-14:TP=-1.0:LRA=9",
}


def target_size(fmt: str, quality: int):
    base = quality
    return {"9:16": (base, base * 16 // 9), "1:1": (base, base), "16:9": (base * 16 // 9, base)}[fmt]


def _ffesc(p: str) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def render_clip(src, info: dict, start: float, end: float, out: Path, opts: dict, words: List[dict], workdir: Path, cancel=None, progress=None) -> dict:
    """Render one clip. opts: format, quality, framing(track|center|blur), caption_style(bold|clean|none), audio(off|clean|studio), face_track bool."""
    dur = end - start
    W, H = target_size(opts["format"], int(opts["quality"]))
    sw, sh = info["width"], info["height"]
    ar_t = W / H
    notes = {"framing_used": opts["framing"], "faces_found": 0}
    chain, cmds_file = [], None
    out_fps = min(info["fps"], 30.0)

    if opts["framing"] == "blur":
        graph = (f"[0:v]split=2[bg][fg];[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:12,eq=brightness=-0.06[b];"
                 f"[fg]scale={W}:{H}:force_original_aspect_ratio=decrease[f];[b][f]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={out_fps:.3f}")
    else:
        # crop window with target aspect inside the source
        if sw / sh > ar_t:
            cw, ch, axis = int(round(sh * ar_t / 2) * 2), sh, 0
        else:
            cw, ch, axis = sw, int(round(sw / ar_t / 2) * 2), 1
        cw, ch = min(cw, sw), min(ch, sh)
        crop_frac = (cw / sw) if axis == 0 else (ch / sh)
        path = None
        if opts["framing"] == "track" and crop_frac < 0.999:
            if progress: progress("Tracking faces…")
            samples = sample_faces(src, start, dur, sw, sh, fps=2.0, cancel=cancel)
            track = build_track(samples)
            notes["faces_found"] = sum(1 for p in track if p)
            path = plan_axis_path(track, axis, 2.0, dur, out_fps, crop_frac)
            if path is None:
                notes["framing_used"] = "center"
        else:
            notes["framing_used"] = "center"
        if path is not None:
            span = (sw - cw) if axis == 0 else (sh - ch)
            key = "x" if axis == 0 else "y"
            cmds_file = workdir / f"{out.stem}_crop.cmd"
            cmds_file.write_text("".join(f"{i / out_fps:.4f} crop@c {key} {int(round(v * (sw if axis == 0 else sh) / 2) * 2)};\n" for i, v in enumerate(path)))
            pre = f"sendcmd=f='{_ffesc(cmds_file)}',crop@c={cw}:{ch}:{int((sw - cw) / 2) if axis == 0 else 0}:{int((sh - ch) / 2) if axis == 1 else 0}"
            notes["framing_used"] = "track"
        else:
            pre = f"crop={cw}:{ch}"
        graph = f"{pre},scale={W}:{H}:flags=lanczos,setsar=1,fps={out_fps:.3f},eq=contrast=1.03:saturation=1.05"

    sub_n = 0
    if opts["caption_style"] != "none" and words:
        ass = workdir / f"{out.stem}.ass"
        sub_n = make_ass(words, start, end, W, H, opts["caption_style"], ass)
        if sub_n:
            graph += f",ass='{_ffesc(ass)}'"
    notes["captions"] = sub_n

    if opts["framing"] == "blur":
        fc = graph.replace("[0:v]", "[0:v]", 1) + "[v]"
        vmap = ["-filter_complex", fc, "-map", "[v]"]
    else:
        vmap = ["-vf", graph, "-map", "0:v:0"]
    af = AUDIO_CHAINS.get(opts.get("audio", "clean"), "")
    amap = ["-map", "0:a:0?"] + (["-af", af] if af and info["has_audio"] else [])
    crf = "20" if int(opts["quality"]) >= 1080 else "22"
    cmd = (["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src)] + vmap + amap +
           ["-c:v", "libx264", "-preset", "veryfast", "-crf", crf, "-pix_fmt", "yuv420p", "-profile:v", "high", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
            "-movflags", "+faststart", "-shortest", str(out)])
    run(cmd, timeout=1800, cancel=cancel)
    notes["width"], notes["height"] = W, H
    return notes
