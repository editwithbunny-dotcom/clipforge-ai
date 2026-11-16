"""Validation of Studio Settings. Every field here is consumed by the pipeline."""
from typing import Any, Dict

DEFAULTS = {
    "provider": "openai", "transcription_model": "", "analysis_model": "",
    "categories": ["comedy", "engagement", "important"],
    "min_duration": 30, "max_duration": 60,
    "format": "9:16", "quality": 1080, "framing": "track",
    "caption_style": "bold", "caption_language": "auto", "audio": "clean",
}
CHOICES = {
    "provider": ("openai", "gemini"), "format": ("9:16", "1:1", "16:9"), "quality": (720, 1080),
    "framing": ("track", "center", "blur"), "caption_style": ("bold", "clean", "none"),
    "caption_language": ("auto", "en", "bn", "hi", "ar", "es"), "audio": ("off", "clean", "studio"),
}
DURATION_PRESETS_MIN, DURATION_MAX_LIMIT = 15, 90


class OptionsError(ValueError):
    pass


def validate_options(raw: Dict[str, Any]) -> Dict[str, Any]:
    o = {**DEFAULTS, **{k: v for k, v in (raw or {}).items() if k in DEFAULTS}}
    for k, allowed in CHOICES.items():
        if k in ("quality",):
            try: o[k] = int(o[k])
            except (TypeError, ValueError): raise OptionsError(f"Invalid {k}.")
        if o[k] not in allowed:
            raise OptionsError(f"Invalid value for {k}: {o[k]!r}.")
    cats = o["categories"]
    if not isinstance(cats, list) or not cats or any(c not in ("comedy", "engagement", "important") for c in cats):
        raise OptionsError("Choose at least one clip category (comedy, engagement, important).")
    o["categories"] = [c for c in ("comedy", "engagement", "important") if c in cats]
    try:
        o["min_duration"], o["max_duration"] = int(o["min_duration"]), int(o["max_duration"])
    except (TypeError, ValueError):
        raise OptionsError("Invalid clip duration.")
    if not (DURATION_PRESETS_MIN <= o["min_duration"] <= o["max_duration"] <= DURATION_MAX_LIMIT):
        raise OptionsError("Clip duration must be between 15 and 90 seconds (min <= max).")
    for k in ("transcription_model", "analysis_model"):
        v = str(o.get(k) or "").strip()
        if v and not all(ch.isalnum() or ch in "-._:/" for ch in v) or len(v) > 100:
            raise OptionsError(f"Invalid {k}.")
        o[k] = v
    return o
