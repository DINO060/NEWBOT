from .helpers import *  # noqa

# Lightweight web helpers used by optional modules
import re
from urllib.parse import urlparse


def is_valid_url(text: str) -> bool:
    if not isinstance(text, str) or not text:
        return False
    try:
        parsed = urlparse(text.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False

_FILENAME_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")

def _sanitize_filename(name: str, default: str = "download") -> str:
    name = name.strip() or default
    name = _FILENAME_SAFE_PATTERN.sub("-", name)
    name = re.sub(r"-+", "-", name).strip("-_. ")
    if not name:
        name = default
    return name[:80]

def generate_filename(url: str) -> str:
    if not is_valid_url(url):
        return "download.pdf"
    parsed = urlparse(url)
    path = (parsed.path or "/").rstrip("/")
    last = path.split("/")[-1] if path else ""
    stem = last or parsed.netloc.replace(":", "_")
    if "scribd.com" in (parsed.netloc or "").lower():
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "document":
            stem = parts[2] or stem
    safe_stem = _sanitize_filename(stem, default=parsed.netloc or "download")
    if not safe_stem.lower().endswith(".pdf"):
        safe_stem = f"{safe_stem}.pdf"
    return safe_stem

__all__ = [
    "is_valid_url",
    "generate_filename",
]
