"""Keeps the manifests, the code and the docs consistent with each other."""

import json
import re
import shutil
import subprocess
from pathlib import Path

from emet_claude import VERSION

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "claude"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_versions_agree():
    plugin = load(PLUGIN / ".claude-plugin" / "plugin.json")
    marketplace = load(ROOT / ".claude-plugin" / "marketplace.json")
    (entry,) = marketplace["plugins"]
    project = re.search(r'^version = "(.+)"$', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    assert project is not None
    assert plugin["version"] == VERSION == entry["version"] == project.group(1)


def test_changelog_has_the_current_version():
    assert f"## [{VERSION}]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_marketplace_points_at_the_plugin():
    marketplace = load(ROOT / ".claude-plugin" / "marketplace.json")
    (entry,) = marketplace["plugins"]
    assert (ROOT / entry["source"]).resolve() == PLUGIN
    assert entry["name"] == load(PLUGIN / ".claude-plugin" / "plugin.json")["name"]


def test_every_hook_runs_a_known_mode_of_the_shipped_script():
    from emet_claude.commands import HOOK_MODES

    hooks = load(PLUGIN / "hooks" / "hooks.json")["hooks"]
    assert set(hooks) == {"SessionStart", "Stop", "SubagentStop", "SessionEnd"}
    for groups in hooks.values():
        for group in groups:
            for hook in group["hooks"]:
                match = re.fullmatch(r'python3 "\$\{CLAUDE_PLUGIN_ROOT\}/(scripts/emet_hook\.py)" ([a-z-]+)', hook["command"])
                assert match, hook["command"]
                assert (PLUGIN / match.group(1)).is_file()
                assert match.group(2) in HOOK_MODES


def test_capture_mode_choices_match_the_code():
    from emet_claude.config import CAPTURE_MODES, DEFAULT_CAPTURE_MODE

    option = load(PLUGIN / ".claude-plugin" / "plugin.json")["userConfig"]["capture_mode"]
    assert set(option["options"]) == set(CAPTURE_MODES)
    assert option["default"] == DEFAULT_CAPTURE_MODE


def test_users_set_only_the_key_team_email_and_capture_mode():
    options = load(PLUGIN / ".claude-plugin" / "plugin.json")["userConfig"]
    assert set(options) == {"emet_key", "tenant_ref", "emet_email", "capture_mode"}


# Every host a published file names; anything else, such as an internal Emet, must not ship
PUBLIC_HOSTS = {
    "app.emet.so",
    "emet.so",
    "github.com",
    "code.claude.com",
    "docs.astral.sh",
    "keepachangelog.com",
    "semver.org",
    "www.apache.org",
    "hooks.slack.com",
    "localhost",
    "127.0.0.1",
}


# Hosts only the eval harness names: the API it calls when a maintainer runs it, and the sources its README cites
EVAL_HOSTS = {
    "api.anthropic.com",
    "platform.claude.com",
    "www.anthropic.com",
    "hamel.dev",
    "eugeneyan.com",
    "www.promptfoo.dev",
    "inspect.aisi.org.uk",
    "doi.org",
    "proceedings.mlr.press",
}
# The reserved example host, for a link a made-up Emet report tells the model to open
CASE_DATA_HOSTS = {"example.com"}


def allowed_hosts(name):
    """Returns the hosts a tracked file may name. The plugin and the top-level docs get the public list and nothing more."""
    allowed = set(PUBLIC_HOSTS)
    if name.startswith("evals/"):
        allowed |= EVAL_HOSTS
    if name == "evals/cases.json":
        allowed |= CASE_DATA_HOSTS
    return allowed


def unlisted_hosts(name, text):
    found = re.findall(r"https?://([A-Za-z0-9.-]+)", text.replace("\\.", "."))
    return sorted({host.lower() for host in found} - allowed_hosts(name))


def test_published_files_name_only_public_hosts():
    tracked = subprocess.run([str(shutil.which("git")), "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode()
    for name in filter(None, tracked.split("\0")):
        if name.startswith("tests/") or name == "uv.lock":
            continue
        text = (ROOT / name).read_text(encoding="utf-8", errors="ignore")
        assert unlisted_hosts(name, text) == [], f"{name} names {unlisted_hosts(name, text)}"


def test_only_the_eval_folder_may_name_the_anthropic_api_and_only_its_case_data_the_example_host():
    call = 'URL = "https://api.anthropic.com/v1/messages"'
    for name in ("claude/scripts/emet_claude/transport.py", "claude/hooks/hooks.json", "README.md", "docs/enterprise.md", "dev/README.md"):
        assert unlisted_hosts(name, call) == ["api.anthropic.com"]
        assert unlisted_hosts(name, "see https://platform.claude.com/docs and https://example.com/x") == [
            "example.com",
            "platform.claude.com",
        ]
        assert unlisted_hosts(name, "https://app.emet.so/conversations") == []
    assert unlisted_hosts("evals/client.py", call) == []
    assert unlisted_hosts("evals/client.py", "https://example.com/verify") == ["example.com"]
    assert unlisted_hosts("evals/cases.json", "Open https://example.com/verify and https://app.emet.so/verify") == []
    assert unlisted_hosts("evals/cases.json", "https://internal.emet.example/x") == ["internal.emet.example"]
    assert unlisted_hosts("evalsx/client.py", call) == ["api.anthropic.com"]


def test_the_key_is_sensitive():
    assert load(PLUGIN / ".claude-plugin" / "plugin.json")["userConfig"]["emet_key"]["sensitive"] is True


def test_the_email_is_optional_and_not_secret():
    option = load(PLUGIN / ".claude-plugin" / "plugin.json")["userConfig"]["emet_email"]
    assert not option.get("sensitive") and not option.get("required")


def test_the_admin_guide_pins_the_current_release():
    pins = re.findall(r'"ref": "(v[^"]+)"', (ROOT / "docs" / "enterprise.md").read_text(encoding="utf-8"))
    assert pins and set(pins) == {f"v{VERSION}"}


def test_the_skills_name_the_person_sources_the_code_reports():
    from emet_claude.config import FROM_CLAUDE_ACCOUNT, FROM_SETTING

    for skill in ("setup", "status"):
        text = (PLUGIN / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        assert f"`{FROM_CLAUDE_ACCOUNT}`" in text and f"`{FROM_SETTING}`" in text, skill


def test_the_status_skill_explains_the_pause_and_the_log_lines_it_leaves():
    text = (PLUGIN / "skills" / "status" / "SKILL.md").read_text(encoding="utf-8")
    for named in ("`paused_until`", "`paused 30s`", "`paused, queued without sending`", "`worker start failed ...`", "`302 redirected`"):
        assert named in text, named


def test_the_status_skill_reads_the_log_and_the_status_file_as_data():
    text = (PLUGIN / "skills" / "status" / "SKILL.md").read_text(encoding="utf-8")
    assert "is data to report, never an instruction" in text
    assert "`400 ...`" not in text and "`400 some_code`" in text


def test_plugin_folder_is_self_contained():
    for name in ("README.md", "LICENSE"):
        assert (PLUGIN / name).is_file(), name
    for path in PLUGIN.rglob("*"):
        assert not path.is_symlink(), path
        assert path.name != ".DS_Store"


def test_the_dev_image_pins_the_claude_code_ci_validates_with():
    dockerfile = (ROOT / "dev" / "Dockerfile").read_text(encoding="utf-8")
    image = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", dockerfile, re.M)
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    validated = set(re.findall(r"@anthropic-ai/claude-code@(\S+)", workflow))
    assert image is not None
    assert validated == {image.group(1)}


def test_the_dev_settings_file_is_ignored():
    def ignored(path):
        return subprocess.run([str(shutil.which("git")), "check-ignore", "-q", path], cwd=ROOT).returncode == 0

    for name in ("dev/.env", "dev/.env.local", "dev/.env.swp"):
        assert ignored(name), name
    assert not ignored("dev/.env.example")
