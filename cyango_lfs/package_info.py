"""Plugin name and version for the User-Agent."""

from __future__ import annotations

import re
from pathlib import Path

CLIENT_ID = "cyango-lichtfeld-plugin"


def _read_version() -> str:
    manifest = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return "0.0.0+unknown"
    project = re.search(r"(?ms)^\[project\]\s*$\s*(.*?)(?=^\[|\Z)", text)
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', project.group(1)) if project else None
    return match.group(1) if match else "0.0.0+unknown"


PLUGIN_VERSION = _read_version()
USER_AGENT = f"{CLIENT_ID}/{PLUGIN_VERSION}"
