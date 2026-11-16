"""OpenAI and Google Gemini clients: model discovery, timestamped transcription, JSON analysis.
Keys are sent only in request headers (never in URLs) and are scrubbed from error messages."""
import base64
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

from .config import get_settings

PROVIDERS = ("openai", "gemini")


class ProviderError(RuntimeError):
    pass


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


def _request(method: str, url: str, key: str, headers: dict, retries=3, timeout=180, **kw):
    last = None
    for attempt in range(retries):
        try:
            r = requests.request(method, url, headers=headers, timeout=timeout, **kw)
        except requests.RequestException as e:
            last = ProviderError(f"Network error talking to the AI provider: {_scrub(str(e), key)[:300]}")
            time.sleep(2 ** attempt); continue
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(min(2 ** (attempt + 1), 20)); last = r; continue
        if r.status_code == 401 or r.status_code == 403:
            raise ProviderError("The AI provider rejected the API key (unauthorized). Check the key and its permissions.")
        if not r.ok:
            detail = ""
            try:
                detail = r.json().get("error", {}).get("message", "") or r.text
            except Exception:
                detail = r.text
            raise ProviderError(f"AI provider error {r.status_code}: {_scrub(detail, key)[:400]}")
        return r
    if isinstance(last, ProviderError):
        raise last
    raise ProviderError("The AI provider is rate-limiting or unavailable. Try again shortly.")


def _oa_headers(key): return {"Authorization": f"Bearer {key}"}
def _gm_headers(key): return {"x-goog-api-key": key, "Content-Type": "application/json"}


# ------------------------------------------------------------------ model discovery
def list_models(provider: str, key: str) -> Dict[str, List[str]]:
    s = get_settings()
    if provider == "openai":
        r = _request("GET", f"{s.openai_base}/models", key, _oa_headers(key), retries=2, timeout=30)
        ids = sorted({m["id"] for m in r.json().get("data", [])})
        trans = [i for i in ids if "whisper" in i]
        analysis = [i for i in ids if re.match(r"^(gpt-|o\d|chatgpt)", i) and not re.search(r"(audio|realtime|transcribe|tts|image|embedding|moderation|search|instruct|codex|vision-preview)", i)]
        return {"transcription": trans, "analysis": analysis}
    if provider == "gemini":
        models, token = [], None
        for _ in range(5):
            r = _request("GET", f"{s.gemini_base}/models", key, _gm_headers(key), retries=2, timeout=30, params={"pageSize": 200, **({"pageToken": token} if token else {})})
            d = r.json(); models += d.get("models", []); token = d.get("nextPageToken")
            if not token: break
        ids = sorted({m["name"].split("/", 1)[-1] for m in models if "generateContent" in m.get("supportedGenerationMethods", [])
                      and not re.search(r"(embedding|imagen|image|tts|aqa|veo|live|robotics|computer-use)", m["name"])})
        return {"transcription": ids, "analysis": ids}
    raise ProviderError("Unsupported provider.")


def prefer(models: List[str], patterns: List[str]) -> Optional[str]:
    for p in patterns:
        hit = [m for m in models if re.search(p, m)]
        if hit:
            return sorted(hit, key=lambda m: (len(m), m))[0]
    return models[0] if models else None


# ------------------------------------------------------------------ transcription
LANG_NAMES = {"en": "English", "bn": "Bangla (Bengali script)", "hi": "Hindi", "ar": "Arabic", "es": "Spanish"}


def transcribe(provider: str, key: str, model: str, audio: Path, language: str = "auto", chunk_len: float = 600.0) -> dict:
    """Returns {"words":[{start,end,text}], "segments":[{start,end,text}], "text":str} with chunk-relative timestamps."""
    s = get_settings()
    if provider == "openai":
        if audio.stat().st_size > 24 * 1024 * 1024:
            raise ProviderError("Audio chunk exceeds the 25 MB provider limit.")
        if "whisper" not in model:
            raise ProviderError(f"Model '{model}' does not return word/segment timestamps, which clipping needs. Choose a whisper model.")
        data = [("model", model), ("response_format", "verbose_json"), ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment")]
        if language not in ("auto", "", None):
            data.append(("language", language))
        with open(audio, "rb") as f:
            r = _request("POST", f"{s.openai_base}/audio/transcriptions", key, _oa_headers(key), timeout=900, data=data, files={"file": (audio.name, f, "audio/mpeg")})
        j = r.json()
        words = [{"start": float(w["start"]), "end": float(w["end"]), "text": w["word"].strip()} for w in j.get("words", []) if w.get("word", "").strip()]
        segs = [{"start": float(x["start"]), "end": float(x["end"]), "text": x["text"].strip()} for x in j.get("segments", []) if x.get("text", "").strip()]
        return {"words": words, "segments": segs or _segments_from_words(words), "text": j.get("text", "")}
    if provider == "gemini":
        raw = audio.read_bytes()
        if len(raw) > 14 * 1024 * 1024:
            raise ProviderError("Audio chunk too large for inline Gemini upload.")
        lang = LANG_NAMES.get(language, "the original spoken language (keep the original script)")
        prompt = (f"Transcribe this audio verbatim in {lang}. The audio is {chunk_len:.0f} seconds or less. Return ONLY JSON: "
                  '{"segments":[{"start":<seconds from audio start, number>,"end":<seconds>,"text":"<sentence-length caption>"}]}. '
                  "Segments must be in order, 2-12 seconds each, non-overlapping, with timestamps as accurate as possible. Do not invent speech; skip silence and music.")
        body = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "audio/mp3", "data": base64.b64encode(raw).decode()}}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
        r = _request("POST", f"{s.gemini_base}/models/{model}:generateContent", key, _gm_headers(key), timeout=900, json=body)
        obj = _gemini_json(r.json())
        segs = []
        for x in obj.get("segments", []):
            try:
                a, b, t = float(x["start"]), float(x["end"]), str(x["text"]).strip()
            except (KeyError, TypeError, ValueError):
                continue
            if t and b > a and a >= 0:
                segs.append({"start": a, "end": min(b, chunk_len + 5), "text": t})
        segs.sort(key=lambda z: z["start"])
        return {"words": _words_from_segments(segs), "segments": segs, "text": " ".join(x["text"] for x in segs)}
    raise ProviderError("Unsupported provider.")


def _words_from_segments(segs: List[dict]) -> List[dict]:
    out = []
    for sg in segs:
        toks = sg["text"].split()
        if not toks: continue
        total = sum(len(t) + 1 for t in toks); t0 = sg["start"]; span = sg["end"] - sg["start"]
        for tok in toks:
            d = span * (len(tok) + 1) / total
            out.append({"start": t0, "end": t0 + d, "text": tok}); t0 += d
    return out


def _segments_from_words(words: List[dict]) -> List[dict]:
    segs, cur = [], []
    for w in words:
        cur.append(w)
        if re.search(r"[.!?।]$", w["text"]) or len(cur) >= 18:
            segs.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": " ".join(x["text"] for x in cur)}); cur = []
    if cur: segs.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": " ".join(x["text"] for x in cur)})
    return segs


# ------------------------------------------------------------------ JSON generation
def _gemini_json(resp: dict) -> dict:
    cands = resp.get("candidates") or []
    if not cands:
        raise ProviderError("Gemini returned no candidates (the content may have been blocked).")
    parts = cands[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    return _parse_json(text)


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try: return json.loads(m.group(0))
            except json.JSONDecodeError: pass
    raise ProviderError("The AI returned malformed JSON.")


def generate_json(provider: str, key: str, model: str, prompt: str) -> dict:
    s = get_settings()
    if provider == "openai":
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}}
        r = _request("POST", f"{s.openai_base}/chat/completions", key, _oa_headers(key), timeout=240, json=body)
        return _parse_json(r.json()["choices"][0]["message"]["content"] or "")
    if provider == "gemini":
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
        r = _request("POST", f"{s.gemini_base}/models/{model}:generateContent", key, _gm_headers(key), timeout=240, json=body)
        return _gemini_json(r.json())
    raise ProviderError("Unsupported provider.")
