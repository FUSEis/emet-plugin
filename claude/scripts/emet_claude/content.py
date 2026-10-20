"""What leaves the machine: secret redaction, binary placeholders and size clipping."""

from __future__ import annotations

import re
from typing import Any, Callable, List, Pattern, Tuple

# (kind, pattern, group to keep in front of the redaction)
SECRET_PATTERNS: List[Tuple[str, Pattern[str], int]] = [
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
        0,
    ),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), 0),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}"), 0),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"), 0),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"), 0),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), 0),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"), 0),
    ("stripe_key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"), 0),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), 0),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), 0),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), 0),
    # An Emet key: an 8 character prefix, a dot and a 32 character secret
    ("emet_key", re.compile(r"\b[A-Za-z0-9]{8}\.[A-Za-z0-9]{32}\b"), 0),
    ("bearer_token", re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=\-]{16,}"), 1),
    ("url_password", re.compile(r"([a-z][a-z0-9+.\-]*://[^/\s:@]+:)[^/\s@]+(?=@)"), 1),
    # NAME=value where the name says secret, as in .env files and shell exports. The value needs a
    # digit and must not be a call or subscript, so code such as token = request.headers.get(...) survives
    (
        "secret_assignment",
        re.compile(
            r"(?i)(\b[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY)[A-Z0-9_]*\s*[:=]\s*[\"']?)"
            r"(?=[A-Za-z0-9_\-./+=]*\d)[A-Za-z0-9_\-./+=]{12,}(?![A-Za-z0-9_\-./+=(\[])"
        ),
        1,
    ),
]

BINARY_TYPES = ("image", "document")
# A long string of only base64 characters, mixing cases and digits as encoded bytes do, is file content, not text
BASE64_RUN = re.compile(r"^(?=[^a-z]*[a-z])(?=[^A-Z]*[A-Z])(?=[^0-9]*[0-9])[A-Za-z0-9+/\r\n]{4096,}={0,2}\s*$")


def redact(text: str, extra_secrets: Tuple[str, ...] = ()) -> Tuple[str, int]:
    """Returns the text with secrets replaced by [REDACTED:kind], and how many were replaced."""
    count = 0
    for secret in extra_secrets:
        if secret and secret in text:
            count += text.count(secret)
            text = text.replace(secret, "[REDACTED:emet_key]")
    for kind, pattern, keep in SECRET_PATTERNS:

        def replace(match: re.Match[str], kind: str = kind, keep: int = keep) -> str:
            return f"{match.group(keep) if keep else ''}[REDACTED:{kind}]"

        text, replaced = pattern.subn(replace, text)
        count += replaced
    return text, count


def walk_strings(value: Any, transform: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, dict):
        return {key: walk_strings(item, transform) for key, item in value.items()}
    if isinstance(value, list):
        return [walk_strings(item, transform) for item in value]
    return value


def size_label(length: int) -> str:
    # A base64 string encodes three bytes in four characters
    kilobytes = max(1, round(length * 3 / 4 / 1024))
    return f"{kilobytes} KB"


def strip_binary(value: Any) -> Tuple[Any, bool]:
    """Replaces images, PDFs and other base64 blobs with a placeholder. Returns the value and whether anything was removed."""
    removed = False

    def visit(item: Any) -> Any:
        nonlocal removed
        if isinstance(item, dict):
            source = item.get("source")
            if item.get("type") in BINARY_TYPES and isinstance(source, dict) and isinstance(source.get("data"), str):
                removed = True
                media = source.get("media_type") or item["type"]
                return {"type": "text", "text": f"[{item['type']} content not shown: {media}, {size_label(len(source['data']))}]"}
            if isinstance(item.get("base64"), str):
                removed = True
                media = item.get("type") or item.get("media_type") or "binary"
                rest = {key: visit(val) for key, val in item.items() if key != "base64"}
                rest["base64"] = f"[content not shown: {media}, {size_label(len(item['base64']))}]"
                return rest
            return {key: visit(val) for key, val in item.items()}
        if isinstance(item, list):
            return [visit(val) for val in item]
        if isinstance(item, str) and BASE64_RUN.match(item):
            removed = True
            return f"[content not shown: base64, {size_label(len(item))}]"
        return item

    return visit(value), removed


def clip(text: str, limit: int) -> str:
    """Keeps the start and the end, which is where errors and summaries usually are."""
    if len(text) <= limit:
        return text
    head = limit * 3 // 4
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n[… {omitted} characters not shown …]\n{text[-tail:]}"


def strip_nul(value: Any) -> Any:
    """Emet rejects NUL characters anywhere in a payload."""
    return walk_strings(value, lambda text: text.replace("\x00", "") if "\x00" in text else text)
