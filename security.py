"""Password hashing, session tokens, API-key encryption, SSRF-safe URL checks, rate limiting."""
import base64
import hashlib
import hmac
import ipaddress
import os
import re
import socket
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

import jwt
from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

# ---------- passwords (scrypt, stdlib) ----------
_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


def validate_credentials(email: str, password: str) -> None:
    if not EMAIL_RE.match(email or ""):
        raise ValueError("Enter a valid email address.")
    if len(password or "") < 10:
        raise ValueError("Password must be at least 10 characters.")
    if len(password) > 200:
        raise ValueError("Password is too long.")


# ---------- session tokens ----------
def create_token(user_id: int) -> str:
    s = get_settings()
    now = int(time.time())
    return jwt.encode({"sub": str(user_id), "iat": now, "exp": now + s.session_hours * 3600}, s.secret_key, algorithm="HS256")


def read_token(token: str):
    try:
        data = jwt.decode(token, get_settings().secret_key, algorithms=["HS256"])
        return int(data["sub"])
    except Exception:
        return None


# ---------- API key encryption at rest ----------
def _fernet() -> Fernet:
    key = hashlib.sha256(b"clipforge-api-keys|" + get_settings().secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("Stored API key cannot be decrypted (SECRET_KEY changed?). Re-enter your key.") from e


def mask_secret(value: str) -> str:
    return (value[:3] + "…" + value[-4:]) if len(value) > 10 else "••••"


# ---------- SSRF protection ----------
class UnsafeURL(ValueError):
    pass


def check_public_https_url(url: str) -> str:
    """Return the URL if it is HTTPS and every resolved address is publicly routable."""
    p = urlparse(url or "")
    if p.scheme != "https" or not p.hostname:
        raise UnsafeURL("Only HTTPS URLs are accepted.")
    if p.username or p.password:
        raise UnsafeURL("URLs with embedded credentials are not allowed.")
    if p.port not in (None, 443):
        raise UnsafeURL("Only the standard HTTPS port is allowed.")
    try:
        infos = socket.getaddrinfo(p.hostname, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURL(f"Could not resolve host: {p.hostname}") from e
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise UnsafeURL("Private, local or reserved network addresses are not allowed.")
    return url


# ---------- tiny in-process rate limiter ----------
class RateLimiter:
    def __init__(self) -> None:
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_s: int) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_s:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


limiter = RateLimiter()
