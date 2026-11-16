"""Central configuration, read once from environment variables."""
import os
import secrets
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


class Settings:
    def __init__(self) -> None:
        self.env = os.getenv("CLIPFORGE_ENV", "production")
        base = Path(__file__).resolve().parent.parent
        self.data_dir = Path(os.getenv("DATA_DIR", str(base / "storage")))
        self.uploads_dir = self.data_dir / "uploads"
        self.outputs_dir = self.data_dir / "outputs"
        self.db_path = Path(os.getenv("DB_PATH", str(self.data_dir / "clipforge.db")))
        self.frontend_dir = Path(os.getenv("FRONTEND_DIR", str(base / "frontend")))
        if not self.frontend_dir.exists():
            self.frontend_dir = base.parent / "frontend"

        secret = os.getenv("SECRET_KEY", "")
        if not secret:
            if self.env == "production":
                raise RuntimeError(
                    "SECRET_KEY is required in production. Set a long random value "
                    "(it signs login sessions and encrypts saved API keys)."
                )
            secret = secrets.token_urlsafe(48)
        if self.env == "production" and len(secret) < 32:
            raise RuntimeError("SECRET_KEY must be at least 32 characters.")
        self.secret_key = secret

        self.allowed_origins = [o.strip() for o in os.getenv("FRONTEND_ORIGIN", "").split(",") if o.strip()]
        self.cookie_secure = os.getenv("COOKIE_SECURE", "1" if self.env == "production" else "0") == "1"
        self.session_hours = _int("SESSION_HOURS", 24 * 7)
        self.allow_registration = os.getenv("ALLOW_REGISTRATION", "1") == "1"
        self.invite_code = os.getenv("INVITE_CODE", "")

        self.max_upload_bytes = _int("MAX_UPLOAD_MB", 1024) * 1024 * 1024
        self.max_source_seconds = _int("MAX_SOURCE_MINUTES", 120) * 60
        self.max_active_jobs_per_user = _int("MAX_ACTIVE_JOBS_PER_USER", 2)
        self.max_jobs_per_day = _int("MAX_JOBS_PER_DAY", 20)
        self.worker_threads = _int("WORKER_THREADS", 1)
        self.retention_hours = _int("RETENTION_HOURS", 72)
        self.ffmpeg_timeout = _int("FFMPEG_TIMEOUT_SECONDS", 1800)

        self.openai_base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.gemini_base = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.uploads_dir, self.outputs_dir):
            p.mkdir(parents=True, exist_ok=True)


_settings = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings


def reset_settings() -> None:  # used by tests
    global _settings
    _settings = None
