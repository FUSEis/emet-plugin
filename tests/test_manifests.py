"""Keeps the manifests, the code and the docs consistent with each other."""

import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_published_files_name_only_public_hosts():
    tracked = subprocess.run([str(shutil.which("git")), "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode()
    for name in filter(None, tracked.split("\0")):
        if name.startswith("tests/") or name == "uv.lock":
            continue
        text = (ROOT / name).read_text(encoding="utf-8", errors="ignore").replace("\\.", ".")
        for host in re.findall(r"https?://([A-Za-z0-9.-]+)", text):
            assert host.lower() in PUBLIC_HOSTS, f"{name} names {host}"


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


def changelog_check():
    """Loads the script CI runs. It lives outside any package, next to the workflow that calls it."""
    spec = importlib.util.spec_from_file_location("changelog_check", ROOT / ".github" / "scripts" / "changelog_check.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def checked(monkeypatch, paths, labels):
    """Runs the check as CI does: the changed paths on stdin, NUL between them, and the labels in the environment."""
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO("\0".join(paths).encode() + b"\0")))
    monkeypatch.setenv("PR_LABELS", labels)
    return changelog_check().main()


CODE_FILE = "claude/scripts/emet_claude/watch.py"


@pytest.mark.parametrize(
    ("paths", "labels", "code"),
    [
        ([CODE_FILE], "[]", 1),
        ([CODE_FILE, "tests/test_watch.py", "README.md"], '["documentation"]', 1),
        (["claude/scripts/emet_hook.py"], "[]", 1),
        ([CODE_FILE, "CHANGELOG.md"], "[]", 0),
        ([CODE_FILE], '["documentation", "no-changelog"]', 0),
        (["claude/README.md", "claude/hooks/hooks.json", "tests/test_watch.py"], "[]", 0),
        # Only the changelog at the root counts, and only a label of exactly that name
        ([CODE_FILE, "docs/CHANGELOG.md"], "[]", 1),
        ([CODE_FILE], '["no-changelog-needed"]', 1),
        ([CODE_FILE], '"no-changelog"', 1),
        ([CODE_FILE], "not json", 1),
        ([CODE_FILE], "", 1),
        ([], "[]", 0),
    ],
)
def test_a_change_to_the_plugins_code_needs_a_changelog_entry_or_the_label(monkeypatch, capsys, paths, labels, code):
    assert checked(monkeypatch, paths, labels) == code
    said = capsys.readouterr().out
    assert (CODE_FILE in said) is (code == 1 and CODE_FILE in paths)


def test_the_required_check_waits_for_the_changelog_job_and_runs_again_when_a_label_changes():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    triggers = re.search(r"^  pull_request:\n(?:    #.*\n)*    types: \[(.+)\]$", workflow, re.M)
    assert triggers is not None
    assert {"opened", "synchronize", "reopened", "labeled", "unlabeled"} <= set(triggers.group(1).split(", "))
    required = workflow.partition("\n  ci:\n")[2]
    needs = re.search(r"^    needs: \[(.+)\]$", required, re.M)
    assert needs is not None
    # A job the required check does not wait for and test could fail without holding a merge
    jobs = set(re.findall(r"^  ([a-z]+):$", workflow.partition("\njobs:\n")[2], re.M)) - {"ci"}
    assert set(needs.group(1).split(", ")) == jobs and "changelog" in jobs
    for job in jobs:
        assert f'[ "${{{{ needs.{job}.result }}}}" = "success" ]' in required, job
    assert "python3 .github/scripts/changelog_check.py" in workflow
    assert "pull_request_target" not in workflow


def test_contributing_names_the_label_the_changelog_check_reads():
    assert f"`{changelog_check().LABEL}`" in (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
