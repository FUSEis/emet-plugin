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
# An Emet key is an 8 character public prefix, a dot and a secret
KEY_PREFIX_LENGTH = 8
KEY_SECRET_MIN_LENGTH = 16


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
        """Returns the public prefix of an Emet key (the part before the dot) to tell keys apart, and nothing of any other value."""
        prefix, dot, secret = self.api_key.partition(".")
        if dot and len(prefix) == KEY_PREFIX_LENGTH and len(secret) >= KEY_SECRET_MIN_LENGTH:
            return f"{prefix}…"
        return "set" if self.api_key else ""


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
    try:
        parts = urlsplit(endpoint)
        hostname = parts.hostname
    except ValueError:
        return f"The endpoint {endpoint!r} is not a valid URL."
    if parts.scheme == "https" and hostname:
        return None
    if parts.scheme == "http" and hostname in LOCAL_HOSTS:
        return None
    return f"The endpoint must be an https:// URL, not {endpoint!r}."
