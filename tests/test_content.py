import base64

import pytest

from emet_claude import content

SECRETS = [
    ("anthropic_key", "key sk-ant-api03-" + "a" * 40),
    ("openai_key", "OPENAI sk-proj-" + "b" * 40),
    ("aws_access_key", "id AKIAIOSFODNN7EXAMPLE here"),
    ("github_token", "ghp_" + "c" * 36),
    ("github_token", "github_pat_" + "d" * 30),
    ("gitlab_token", "glpat-" + "e" * 20),
    ("slack_token", "xoxb-1234567890-abcdefghij"),
    ("slack_webhook", "https://hooks.slack.com/services/T000/B000/XXXX"),
    ("stripe_key", "sk_live_" + "f" * 24),
    ("google_api_key", "AIza" + "g" * 35),
    ("npm_token", "npm_" + "h" * 36),
    # Built from parts so secret scanners do not mistake the fixture for a real token
    ("jwt", ".".join(["eyJ" + "hbGciOiJub25lIn0", "eyJ" + "zdWIiOiJ0ZXN0In0", "c2lnbmF0dXJlLXZhbHVl"])),
    ("emet_key", "Abcd1234.ZyxwVuts98765432QwertyUiop123456"),
    ("private_key", "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"),
]


@pytest.mark.parametrize("kind,text", SECRETS)
def test_redacts_known_secret_formats(kind, text):
    redacted, count = content.redact(text)
    assert f"[REDACTED:{kind}]" in redacted
    assert count >= 1


def test_redacts_bearer_and_keeps_scheme():
    redacted, _ = content.redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456")
    assert redacted == "Authorization: Bearer [REDACTED:bearer_token]"


def test_redacts_password_in_url_and_keeps_user():
    redacted, _ = content.redact("postgres://app:hunter2secret@db.internal:5432/app")
    assert redacted == "postgres://app:[REDACTED:url_password]@db.internal:5432/app"


@pytest.mark.parametrize(
    "line",
    [
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY1",
        'export GITHUB_TOKEN="abc123def456ghi789"',
        "db_password: Sup3rS3cretValue99",
    ],
)
def test_redacts_secret_assignments(line):
    redacted, count = content.redact(line)
    assert "[REDACTED:" in redacted
    assert count == 1


@pytest.mark.parametrize(
    "code",
    [
        'token = request.headers.get("Authorization")',
        "api_key = settings.EMET_API_KEY",
        "access_token: str = get_access_token(user)",
        "password_reset_enabled = True",
        "def rotate_token(old_token): ...",
    ],
)
def test_leaves_ordinary_code_alone(code):
    assert content.redact(code) == (code, 0)


def test_redacts_the_configured_key_verbatim():
    redacted, count = content.redact("my key is plain-key-value", ("plain-key-value",))
    assert redacted == "my key is [REDACTED:emet_key]"
    assert count == 1


def test_image_parts_become_placeholders():
    data = base64.b64encode(b"x" * 3000).decode()
    value = [
        {"type": "text", "text": "Screenshot"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
    ]
    stripped, removed = content.strip_binary(value)
    assert removed
    assert stripped[0] == value[0]
    assert stripped[1]["type"] == "text"
    assert stripped[1]["text"].startswith("[image content not shown: image/png")


def test_base64_fields_and_blobs_become_placeholders():
    blob = base64.b64encode(b"y" * 6000).decode()
    stripped, removed = content.strip_binary({"file": {"type": "image/jpeg", "base64": blob}, "raw": blob})
    assert removed
    assert stripped["file"]["base64"].startswith("[content not shown: image/jpeg")
    assert stripped["raw"].startswith("[content not shown: base64")


def test_plain_text_is_not_binary():
    value = {"stdout": "hello\n" * 2000}
    assert content.strip_binary(value) == (value, False)


def test_clip_keeps_both_ends_and_says_so():
    text = "a" * 100 + "b" * 100
    clipped = content.clip(text, 40)
    assert clipped.startswith("a" * 30)
    assert clipped.endswith("b" * 10)
    assert "160 characters not shown" in clipped
    assert content.clip("short", 40) == "short"


def test_clean_removes_nul_and_lone_surrogates_everywhere():
    cleaned = content.clean({"a\x00": ["b\x00c", 1, "x\ud800y"]})
    assert cleaned == {"a": ["bc", 1, "x\ufffdy"]}
    "".join(cleaned["a"][::2]).encode("utf-8")


def test_secrets_in_keys_are_redacted():
    key = "ghp_" + "k" * 36
    walked = content.walk_strings({key: "value"}, lambda text: content.redact(text)[0])
    assert list(walked) == ["[REDACTED:github_token]"]


def test_a_jwt_holding_a_key_shaped_run_is_redacted_whole():
    token = "eyJhbGciOiJIUzI1NiJ9-Abcdefgh.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFt-ZSI6IkpvaG4ifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    text, _ = content.redact(f"token {token}")
    assert text == "token [REDACTED:jwt]"
