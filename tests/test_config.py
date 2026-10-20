from emet_claude.config import DEFAULT_ENDPOINT, endpoint_problem, load


def test_reads_plugin_options():
    config = load(
        {
            "CLAUDE_PLUGIN_OPTION_API_KEY": " abc.def ",
            "CLAUDE_PLUGIN_OPTION_TENANT_REF": "team",
            "CLAUDE_PLUGIN_OPTION_ENDPOINT": "https://emet.example.com/",
            "CLAUDE_PLUGIN_OPTION_CAPTURE_MODE": "FULL",
        }
    )
    assert config.api_key == "abc.def"
    assert config.endpoint == "https://emet.example.com"
    assert config.capture_mode == "full"
    assert config.problem is None


def test_defaults():
    config = load({})
    assert config.endpoint == DEFAULT_ENDPOINT
    assert config.capture_mode == "sanitized"
    assert config.problem == "No Emet key is set."


def test_unknown_capture_mode_falls_back_to_sanitized():
    assert load({"CLAUDE_PLUGIN_OPTION_CAPTURE_MODE": "everything"}).capture_mode == "sanitized"


def test_missing_team_is_a_problem():
    assert load({"CLAUDE_PLUGIN_OPTION_API_KEY": "k"}).problem == "No team is set."


def test_endpoint_must_be_https_unless_local():
    assert endpoint_problem("https://app.emet.so") is None
    assert endpoint_problem("http://localhost:8000") is None
    assert endpoint_problem("http://127.0.0.1:8000") is None
    assert endpoint_problem("http://app.emet.so") is not None
    assert endpoint_problem("ftp://app.emet.so") is not None
    assert endpoint_problem("app.emet.so") is not None


def test_key_hint_never_shows_the_secret():
    config = load({"CLAUDE_PLUGIN_OPTION_API_KEY": "Abcd1234.secretsecret"})
    assert config.key_hint == "Abcd1234…"
    assert "secret" not in config.key_hint
