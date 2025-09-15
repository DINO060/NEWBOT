"""Utilities for the PDF bot.

This module exposes helpers used across handlers without adding heavy deps.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse


def is_valid_url(text: str) -> bool:
    """Return True if text looks like an http(s) URL with a host.

    Conservative validation to avoid false positives in chat messages.
    """
    if not isinstance(text, str) or not text:
        return False
    try:
        parsed = urlparse(text.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


_FILENAME_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename(name: str, default: str = "download") -> str:
    """Make a safe filename stem from arbitrary text.

    - Replace unsupported chars with '-'
    - Collapse repeats, strip separators
    - Truncate to a reasonable length
    """
    name = name.strip() or default
    name = _FILENAME_SAFE_PATTERN.sub("-", name)
    name = re.sub(r"-+", "-", name).strip("-_. ")
    if not name:
        name = default
    return name[:80]


def generate_filename(url: str) -> str:
    """Generate a deterministic, safe PDF filename from a URL.

    Examples:
      https://example.com/docs/file.pdf -> file.pdf
      https://scribd.com/document/12345/Title -> Title.pdf
      https://host/path/ -> host_path.pdf
    """
    if not is_valid_url(url):
        return "download.pdf"

    parsed = urlparse(url)
    path = (parsed.path or "/").rstrip("/")

    # Prefer the last path segment if present
    last = path.split("/")[-1] if path else ""
    stem = last or parsed.netloc.replace(":", "_")

    # Special-case Scribd: keep human-readable title segment if present
    if "scribd.com" in (parsed.netloc or "").lower():
        parts = [p for p in path.split("/") if p]
        # Typical: /document/<id>/<title>
        if len(parts) >= 3 and parts[0] == "document":
            stem = parts[2] or stem

    safe_stem = _sanitize_filename(stem, default=parsed.netloc or "download")

    # Ensure .pdf extension
    if not safe_stem.lower().endswith(".pdf"):
        safe_stem = f"{safe_stem}.pdf"

    return safe_stem


__all__ = [
    "is_valid_url",
    "generate_filename",
]

