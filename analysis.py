"""Clip selection: candidate windows -> LLM scoring on Comedy / Engagement / Important -> best non-overlapping clip per category."""
import math
import re
from typing import Dict, List, Optional

from . import providers

CATEGORIES = ("comedy", "engagement", "important")
LABELS = {"comedy": "Comedy", "engagement": "Engagement", "important": "Important Moment"}
LAUGH = re.compile(r"(ha(ha)+|lol|lmao|laugh|joke|funny|hilarious|😂|🤣|হা(হা)+|হাসি|মজা)", re.I)
HOOK = re.compile(r"(\?|!|\bwait\b|\bsecret\b|\bnever\b|\bmistake\b|\bshocking\b|\bactually\b|\bcrazy\b|\bwhy\b|\bhow\b|\d+)", re.I)
IMPORTANT = re.compile(r"(important|key|remember|lesson|because|therefore|the truth|the reason|conclusion|how to|step|rule|problem|solution|কারণ|গুরুত্বপূর্ণ)", re.I)


def merge_chunks(results: List[dict], offsets: List[float]) -> dict:
    words, segs = [], []
    for res, off in zip(results, offsets):
        words += [{**w, "start": w["start"] + off, "end": w["end"] + off} for w in res["words"]]
        segs += [{**s, "start": s["start"] + off, "end": s["end"] + off} for s in res["segments"]]
    words.sort(key=lambda x: x["start"]); segs.sort(key=lambda x: x["start"])
    return {"words": words, "segments": segs}


def _text(segs, a, b) -> str:
    return " ".join(s["text"] for s in segs if s["end"] > a and s["start"] < b)


def build_candidates(segs: List[dict], duration: float, scenes: List[float], loud: List[float], min_d: float, max_d: float, limit=36) -> List[dict]:
    """Windows start at a segment start and end at a segment end within [min_d, max_d] so clips never cut mid-sentence."""
    if not segs: return []
    cands = []
    starts = segs[::1]
    for i, s0 in enumerate(starts):
        a = s0["start"]
        best_end = None
        for s1 in segs[i:]:
            d = s1["end"] - a
            if d > max_d: break
            if d >= min_d: best_end = s1["end"]
        if best_end is None: continue
        b = best_end
        text = _text(segs, a, b)
        words = len(re.findall(r"\w+", text, re.U))
        if words < 12: continue
        sl = loud[int(a):int(math.ceil(b))] if loud else []
        peaks = sum(1 for v in sl if v > 0.6)
        energy = (sum(sl) / len(sl)) if sl else 0.0
        opening = _text(segs, a, a + 6)
        feats = {"laugh": len(LAUGH.findall(text)), "hook": len(HOOK.findall(opening)), "imp": len(IMPORTANT.findall(text)),
                 "density": min(words / ((b - a) * 3.2), 1.5), "peaks": peaks / max(len(sl), 1), "energy": energy,
                 "scenes": sum(1 for t in scenes if a < t < b)}
        h = {"comedy": feats["laugh"] * 2.5 + feats["peaks"] * 4 + feats["energy"] * 2 + feats["density"] * 0.5,
             "engagement": feats["hook"] * 1.4 + feats["density"] * 2 + feats["energy"] * 2.5 + min(feats["scenes"], 4) * 0.3,
             "important": feats["imp"] * 1.6 + feats["density"] * 2 + words / 90}
        cands.append({"start": a, "end": b, "text": text, "heur": h, "feats": feats})
    # keep a diverse top set: top by each heuristic, then fill by overall
    chosen, seen = [], set()
    for cat in CATEGORIES:
        for c in sorted(cands, key=lambda x: -x["heur"][cat]):
            if id(c) in seen: continue
            if all(_overlap(c, o) < 0.5 for o in chosen):
                chosen.append(c); seen.add(id(c))
            if len([x for x in chosen if x.get("_cat") == cat]) >= limit // 3: break
            c["_cat"] = cat
    for c in cands:  # top-up
        if len(chosen) >= limit: break
        if id(c) not in seen and all(_overlap(c, o) < 0.5 for o in chosen):
            chosen.append(c); seen.add(id(c))
    chosen.sort(key=lambda x: x["start"])
    return chosen[:limit]


def _overlap(a, b) -> float:
    inter = min(a["end"], b["end"]) - max(a["start"], b["start"])
    return max(0.0, inter) / max(1e-9, min(a["end"] - a["start"], b["end"] - b["start"]))


def llm_score(cands: List[dict], provider: str, key: str, model: str, language_hint: str) -> List[dict]:
    compact = [{"id": i, "start": round(c["start"], 1), "end": round(c["end"], 1), "text": c["text"][:1100]} for i, c in enumerate(cands)]
    prompt = (
        "You are a senior short-form video editor choosing clips from a long video for Shorts/Reels/TikTok. "
        "For EACH candidate transcript window below, score from 0 to 10:\n"
        "- comedy: genuinely funny (jokes, punchlines, funny reactions, laughter, absurd or witty moments)\n"
        "- engagement: strong hook in the first seconds, curiosity, emotion, surprise, controversy, high retention potential\n"
        "- important: the most valuable information, key insight, turning point, or main message, self-contained and understandable alone\n"
        "Also give: title (max 8 words, in the transcript's language, no clickbait lies) and reason (one sentence, English). "
        "Be critical: most windows should score low; reserve 8+ for truly standout moments. Judge only from the text; do not invent content. "
        'Return ONLY JSON: {"scores":[{"id":0,"comedy":0,"engagement":0,"important":0,"title":"","reason":""}]}\n'
        f"Language hint: {language_hint}\nCandidates:\n" + _dump(compact))
    obj = providers.generate_json(provider, key, model, prompt)
    by_id = {}
    for x in obj.get("scores", []):
        try:
            i = int(x["id"])
            if 0 <= i < len(cands):
                by_id[i] = {"comedy": _num(x.get("comedy")), "engagement": _num(x.get("engagement")), "important": _num(x.get("important")),
                            "title": str(x.get("title", ""))[:90], "reason": str(x.get("reason", ""))[:240]}
        except (KeyError, TypeError, ValueError):
            continue
    if len(by_id) < max(1, len(cands) // 3):
        raise providers.ProviderError("The AI returned too few usable scores.")
    for i, c in enumerate(cands):
        c["ai"] = by_id.get(i)
    return cands


def _dump(x) -> str:
    import json
    return json.dumps(x, ensure_ascii=False)


def _num(v) -> float:
    try: return max(0.0, min(10.0, float(v)))
    except (TypeError, ValueError): return 0.0


def select_clips(cands: List[dict], categories: List[str], use_ai: bool) -> List[dict]:
    """Exact search for the best assignment of one distinct, non-overlapping window per category (<=3 categories x <=36 windows),
    maximizing the summed category scores. Greedy picking can let one broad window steal two categories' best moments."""
    def score(c, cat):
        ai = c.get("ai") if use_ai else None
        base = ai[cat] * 10 + c["heur"][cat] * 0.1 if ai else c["heur"][cat]
        return base - 0.01 * (c["end"] - c["start"])  # tie-break toward tighter clips

    best = {"total": -1e18, "combo": None}

    def rec(i, chosen, total):
        if i == len(categories):
            if total > best["total"]: best.update(total=total, combo=list(chosen))
            return
        for c in cands:
            if any(_overlap(c, p) > 0.2 for p, _ in chosen): continue
            chosen.append((c, categories[i])); rec(i + 1, chosen, total + score(c, categories[i])); chosen.pop()

    rec(0, [], 0.0)
    if not best["combo"]:
        return []
    picked = []
    for c, cat in best["combo"]:
        ai = c.get("ai") if use_ai else None
        picked.append({**c, "category": cat, "label": LABELS[cat], "score": round(ai[cat], 1) if ai else round(c["heur"][cat], 2),
                       "title": (ai or {}).get("title") or f"{LABELS[cat]} moment",
                       "reason": (ai or {}).get("reason") or "Selected by speech, loudness and scene-change analysis.",
                       "method": "ai" if ai else "heuristic"})
    order = {c: i for i, c in enumerate(CATEGORIES)}
    return sorted(picked, key=lambda p: order[p["category"]])
