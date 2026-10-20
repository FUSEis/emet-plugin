"""Reads the plugin options Claude Code exports to hooks."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urlsplit

DEFAULT_ENDPOINT = "https://app.emet.so"
CAPTURE_MODES = ("sanitized", "full", "metadata")
DEFAULT_CAPTURE_MODE = "sanitized"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class Config:
    api_key: str
    tenant_ref: str
    endpoint: str
    capture_mode: str

    @property
    def problem(self) -> Optional[str]:
        """Returns why the plugin cannot send, or None when it can."""
        if not self.api_key:
            return "No Emet key is set."
        if not self.tenant_ref:
            return "No team is set."
        return endpoint_problem(self.endpoint)

    @property
    def key_hint(self) -> str:
        """Returns enough of the key to tell keys apart, never the secret part."""
        prefix = self.api_key.split(".", 1)[0]
        return f"{prefix[:8]}…" if prefix else ""


def option(name: str, environ: Mapping[str, str]) -> str:
    return (environ.get(f"CLAUDE_PLUGIN_OPTION_{name}") or "").strip()


def load(environ: Mapping[str, str] = os.environ) -> Config:
    mode = option("CAPTURE_MODE", environ).lower()
    return Config(
        api_key=option("API_KEY", environ),
        tenant_ref=option("TENANT_REF", environ),
        endpoint=(option("ENDPOINT", environ) or DEFAULT_ENDPOINT).rstrip("/"),
        capture_mode=mode if mode in CAPTURE_MODES else DEFAULT_CAPTURE_MODE,
    )


def endpoint_problem(endpoint: str) -> Optional[str]:
    """Requires HTTPS, except on this machine, so traces never cross a network in clear text."""
    parts = urlsplit(endpoint)
    if parts.scheme == "https" and parts.hostname:
        return None
    if parts.scheme == "http" and parts.hostname in LOCAL_HOSTS:
        return None
    return f"The endpoint must be an https:// URL, not {endpoint!r}."
