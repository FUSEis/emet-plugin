from emet_claude.config import DEFAULT_ENDPOINT, endpoint_problem, load


def test_reads_plugin_options():
    config = load(
        {
            "CLAUDE_PLUGIN_OPTION_EMET_KEY": " abc.def ",
            "CLAUDE_PLUGIN_OPTION_TENANT_REF": "team",
            "CLAUDE_PLUGIN_OPTION_ENDPOINT": "http://localhost:8000/",
            "CLAUDE_PLUGIN_OPTION_CAPTURE_MODE": "FULL",
        }
    )
    assert config.api_key == "abc.def"
    assert config.endpoint == "http://localhost:8000"
    assert config.capture_mode == "full"
    assert config.problem is None


def test_defaults():
    config = load({})
    assert config.endpoint == DEFAULT_ENDPOINT == "https://app.emet.so"
    assert config.capture_mode == "sanitized"
    assert config.problem == "No Emet key is set."


def test_unknown_capture_mode_falls_back_to_sanitized():
    assert load({"CLAUDE_PLUGIN_OPTION_CAPTURE_MODE": "everything"}).capture_mode == "sanitized"


def test_missing_team_is_a_problem():
    assert load({"CLAUDE_PLUGIN_OPTION_EMET_KEY": "k"}).problem == "No team is set."


def test_only_emet_or_a_local_emet_receives_traces():
    assert endpoint_problem("https://app.emet.so") is None
    assert endpoint_problem("http://localhost:8000") is None
    assert endpoint_problem("http://127.0.0.1:8000") is None
    assert endpoint_problem("http://[::1]:8000") is None
    assert "local Emet" in endpoint_problem("https://collector.example.com")
    assert endpoint_problem("https://app.emet.so.example.com") is not None
    assert endpoint_problem("http://app.emet.so") is not None
    assert endpoint_problem("ftp://app.emet.so") is not None
    assert endpoint_problem("app.emet.so") is not None


def test_key_hint_never_shows_the_secret():
    config = load({"CLAUDE_PLUGIN_OPTION_EMET_KEY": "Abcd1234.secretsecretsecret12"})
    assert config.key_hint == "Abcd1234…"
    assert "secret" not in config.key_hint


def test_key_hint_shows_nothing_of_an_unexpected_key():
    for value in ("short", "hunter2hunter2", "abc.def", "Abcd1234.short"):
        assert load({"CLAUDE_PLUGIN_OPTION_EMET_KEY": value}).key_hint == "set"
    assert load({}).key_hint == ""


def test_malformed_endpoint_is_a_problem_not_a_crash():
    assert "not a valid URL" in endpoint_problem("https://[::1")


def test_a_typed_email_is_the_person():
    config = load({"CLAUDE_PLUGIN_OPTION_USER_EMAIL": " ada@example.com "})
    assert (config.enduser_ref, config.enduser_source) == ("ada@example.com", "setting")
    assert load({}).enduser_ref == ""
