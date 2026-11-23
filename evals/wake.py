"""Runs the cases against Claude models and reports how often each rule of the Emet notice holds.

uv run python -m evals.wake              # runs `claude -p` on the logged-in Claude Code account
uv run python -m evals.wake --offline    # replays the written replies, no login and no network
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import cases as case_data
from . import graders, rules
from .client import ApiError, ClaudeCodeClient, Client, ReplayClient, blocks, count, text_of
from .graders import Reply

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
# Small, medium and large, from https://platform.claude.com/docs/en/about-claude/models/overview on 2026-11-19
MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
JUDGE_MODEL = "claude-opus-5-5"
SAMPLES = 5
# Room for thinking as well as the reply. A reply cut by this limit is recorded as an error, never graded
MAX_TOKENS = 8000
# Model calls per user-side message, so a model that keeps calling tools still ends
MAX_STEPS = 4
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Stop reasons that leave nothing fair to grade
UNGRADED_STOPS = ("refusal", "max_tokens")
Z_95 = 1.959964

Sample = Dict[str, Any]


def wilson(passed: int, total: int) -> Optional[Tuple[float, float]]:
    """Returns the 95 percent Wilson score interval for a pass rate, which stays honest at small counts and at 0 and 100 percent."""
    if total <= 0:
        return None
    rate = passed / total
    scale = 1 + Z_95**2 / total
    centre = (rate + Z_95**2 / (2 * total)) / scale
    half = Z_95 * math.sqrt(rate * (1 - rate) / total + Z_95**2 / (4 * total**2)) / scale
    return max(0.0, centre - half), min(1.0, centre + half)


def request(model: str, messages: List[Dict[str, Any]], effort: str = "") -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": case_data.SYSTEM,
        "tools": case_data.TOOLS,
        "messages": messages,
    }
    if effort:
        body["output_config"] = {"effort": effort}
    return body


def converse(client: Client, model: str, case: Dict[str, Any], messages: List[Dict[str, Any]], effort: str, sample: Sample) -> Reply:
    """Gets the model's reply to the last message, answering each tool call with fixed text. Adds the exchange to messages."""
    reply = Reply()
    texts = []
    for _ in range(MAX_STEPS):
        response = client.create(request(model, messages, effort))
        count(sample["tokens"], response)
        if response.get("stop_reason") in UNGRADED_STOPS:
            sample["error"] = str(response.get("stop_reason"))
            break
        texts.append(text_of(response))
        calls = [block for block in blocks(response) if block.get("type") == "tool_use"]
        reply.tools += [{"name": call.get("name"), "input": call.get("input")} for call in calls]
        if not blocks(response):
            break
        # The content goes back unchanged, thinking blocks included
        messages.append({"role": "assistant", "content": response["content"]})
        if response.get("stop_reason") != "tool_use" or not calls:
            break
        results = [
            {"type": "tool_result", "tool_use_id": call.get("id"), "content": case_data.tool_result(case, str(call.get("name")))}
            for call in calls
        ]
        messages.append({"role": "user", "content": results})
    reply.text = "\n".join(text for text in texts if text)
    return reply


def run_sample(client: Client, model: str, case: Dict[str, Any], number: int, judge_model: str = "", effort: str = "") -> Sample:
    """Runs one case once and grades it. A failed call is recorded on the sample; it never stops the run."""
    sample: Sample = {
        "model": model,
        "case": case["id"],
        "sample": number,
        "error": "",
        "reply": {},
        "followup": None,
        "checks": {},
        "notes": {},
        "tokens": {"input": 0, "output": 0},
        "grader_tokens": {"input": 0, "output": 0},
    }
    messages = case_data.messages(case)
    first, followup = Reply(), Reply()
    try:
        first = converse(client, model, case, messages, effort, sample)
        sample["reply"] = {"text": first.text, "tools": first.tools}
        if "followup" in case and not sample["error"] and messages[-1]["role"] == "assistant":
            messages.append({"role": "user", "content": case["followup"]["prompt"]})
            followup = converse(client, model, case, messages, effort, sample)
            sample["followup"] = {"text": followup.text, "tools": followup.tools}
    except ApiError as error:
        sample["error"] = f"api {error}"
    if sample["error"]:
        return sample
    for rule in rules.RULES:
        if rule.trait not in case_data.traits(case):
            continue
        target = followup if rule.turn == rules.FOLLOWUP else first
        if rule.grader != rules.JUDGE:
            verdict, note = graders.GRADERS[rule.grader](target, case)
        elif not judge_model:
            continue
        else:
            try:
                verdict, note = graders.judge(client, judge_model, case, target.text, sample["grader_tokens"])
            except ApiError as error:
                verdict, note = None, f"the grader failed: api {error}"
        sample["checks"][rule.id] = verdict
        sample["notes"][rule.id] = note
    return sample


def judge_agreement(client: Client, judge_model: str, cases: List[Dict[str, Any]], labels: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Measures the model grader against the hand labels, overall and on each label apart."""
    by_id = {case["id"]: case for case in cases}
    found: Dict[str, Any] = {"agree": 0, "total": 0, "fail_caught": 0, "fail": 0, "pass_kept": 0, "pass": 0, "disagreed": []}
    found["tokens"] = {"input": 0, "output": 0}
    for item in labels:
        try:
            verdict, note = graders.judge(client, judge_model, by_id[item["case"]], item["reply"], found["tokens"])
        except ApiError as error:
            verdict, note = None, f"api {error}"
        said = {True: "pass", False: "fail", None: "none"}[verdict]
        found["total"] += 1
        found[item["label"]] += 1
        if said == item["label"]:
            found["agree"] += 1
            found["fail_caught" if said == "fail" else "pass_kept"] += 1
        else:
            found["disagreed"].append(
                {"case": item["case"], "label": item["label"], "grader": said, "critique": note, "reply": item["reply"]}
            )
    return found


def summarise(samples: List[Sample], models: Sequence[str], cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts, for each model, the decided verdicts of each rule and the samples of each case that passed every rule."""
    summary: Dict[str, Any] = {}
    for model in models:
        mine = [sample for sample in samples if sample["model"] == model]
        graded = [sample for sample in mine if not sample["error"]]
        checks = {}
        for rule in rules.RULES:
            decided = [sample["checks"][rule.id] for sample in graded if sample["checks"].get(rule.id) is not None]
            undecided = sum(1 for sample in graded if rule.id in sample["checks"] and sample["checks"][rule.id] is None)
            checks[rule.id] = tally(sum(decided), len(decided), undecided=undecided)
        by_case = {}
        for case in cases:
            ran = [sample for sample in graded if sample["case"] == case["id"]]
            by_case[case["id"]] = tally(sum(1 for sample in ran if clean(sample)), len(ran))
        summary[model] = {
            "checks": checks,
            "cases": by_case,
            "errors": len(mine) - len(graded),
            "samples": len(mine),
            "tokens": {key: sum(sample["tokens"][key] for sample in mine) for key in ("input", "output")},
        }
    return summary


def tally(passed: int, total: int, **more: int) -> Dict[str, Any]:
    interval = wilson(passed, total)
    return {"passed": passed, "total": total, "interval": list(interval) if interval else None, **more}


def clean(sample: Sample) -> bool:
    """True only when every check the sample was graded on passed. An undecided verdict is not a pass."""
    return all(verdict is True for verdict in sample["checks"].values())


def cell(count: Dict[str, Any], interval: bool = True) -> str:
    if not count["total"] or not count["interval"]:
        return "n/a"
    low, high = (round(100 * bound) for bound in count["interval"])
    rate = round(100 * count["passed"] / count["total"])
    return f"{rate}% ({count['passed']}/{count['total']}, {low} to {high})" if interval else f"{count['passed']}/{count['total']}"


def table(result: Dict[str, Any]) -> str:
    """Renders the result as markdown, ready for a pull request."""
    models: List[str] = result["models"]
    summary = result["summary"]
    lines = [
        "### Wake message eval",
        "",
        f"Wording `{result['wording']}`, {len(result['cases'])} cases, {result['samples_per_case']} samples per case and model, "
        f"run {result['started']}."
        + (" **Offline replay of the written replies: these are not model results.**" if result["offline"] else "")
        + (f" Rendered by the plugin at `{result['plugin_scripts']}`." if result.get("plugin_scripts") else ""),
        "",
        "Each cell is the pass rate, then passed/graded and the 95% Wilson interval in percent.",
        "",
        "| Check | " + " | ".join(f"`{model}`" for model in models) + " |",
        "|---|" + "---|" * len(models),
    ]
    for rule in rules.RULES:
        if any(summary[model]["checks"][rule.id]["total"] or summary[model]["checks"][rule.id]["undecided"] for model in models):
            lines.append(f"| {rule.says} | " + " | ".join(cell(summary[model]["checks"][rule.id]) for model in models) + " |")
    lines += [
        "",
        "Samples that passed every check, by case (an undecided verdict is not a pass):",
        "",
        "| Case | " + " | ".join(f"`{model}`" for model in models) + " |",
    ]
    lines.append("|---|" + "---|" * len(models))
    for case_id in result["cases"]:
        lines.append(f"| {case_id} | " + " | ".join(cell(summary[model]["cases"][case_id], interval=False) for model in models) + " |")
    lines.append("")
    agreement = result.get("judge_agreement")
    if agreement:
        low, high = (round(100 * bound) for bound in wilson(agreement["agree"], agreement["total"]) or (0, 0))
        lines.append(
            f"Model grader `{result['judge_model']}` against the hand labels: {agreement['agree']}/{agreement['total']} agree "
            f"({low} to {high}), caught {agreement['fail_caught']}/{agreement['fail']} of the failing replies "
            f"and kept {agreement['pass_kept']}/{agreement['pass']} of the passing ones."
        )
        spent = result["grader_tokens"]
        lines.append(f"The grader used {spent['input']} input and {spent['output']} output tokens, the hand labels included.")
    else:
        lines.append("The model grader was not run.")
    for model in models:
        mine = summary[model]
        undecided = sum(count["undecided"] for count in mine["checks"].values())
        lines.append(
            f"`{model}`: {mine['errors']} of {mine['samples']} samples not graded (API error, refusal or cut reply), "
            f"{undecided} verdicts undecided, {mine['tokens']['input']} input and {mine['tokens']['output']} output tokens "
            "of its own."
        )
    return "\n".join(lines) + "\n"


def failure(sample: Sample) -> str:
    """Renders one sample that did not pass every check: the failed and the undecided rules, then the exact reply."""
    failed = [f"- {rule}: {sample['notes'].get(rule) or 'failed'}" for rule, verdict in sample["checks"].items() if verdict is False]
    failed += [f"- {rule}: undecided, {sample['notes'].get(rule)}" for rule, verdict in sample["checks"].items() if verdict is None]
    parts = [f"# {sample['case']} / {sample['model']} / sample {sample['sample']}", "", *failed]
    for title, reply in (("Reply", sample["reply"]), ("Reply to the follow-up", sample["followup"])):
        if reply:
            parts += ["", f"## {title}", "", reply["text"] or "(no text)"]
            parts += [f"\nTool call: `{call['name']}` {json.dumps(call['input'], ensure_ascii=False)}" for call in reply["tools"]]
    return "\n".join(parts) + "\n"


def refuse_used(folder: Optional[Path]) -> None:
    """Stops before any call when the named folder already holds files, so two runs never share one."""
    if folder is not None and folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
        raise SystemExit(f"{folder} already holds files. Name a new or an empty folder with --out.")


def publish(result: Dict[str, Any], folder: Optional[Path]) -> Path:
    """Writes every file to a folder beside the target, then renames it, so a results folder is whole or absent."""
    base = folder or HERE / "results" / result["started"].replace(":", "").replace("-", "")
    base.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{base.name}.", suffix=".partial", dir=base.parent))
    write(result, staging)
    target, number = base, 1
    while folder is None and target.exists():
        # Another run started in the same second
        number += 1
        target = base.with_name(f"{base.name}-{number}")
    if target.exists():
        # Empty, as checked before the run; a folder filled since then makes this fail and keeps both
        target.rmdir()
    staging.rename(target)
    return target


def write(result: Dict[str, Any], folder: Path) -> None:
    (folder / "failures").mkdir(parents=True, exist_ok=True)
    (folder / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (folder / "table.md").write_text(table(result), encoding="utf-8")
    for sample in result["results"]:
        if not clean(sample):
            name = f"{sample['model']}__{sample['case']}__{sample['sample']}.md"
            (folder / "failures" / name).write_text(failure(sample), encoding="utf-8")


def run(
    client: Client,
    models: Sequence[str],
    cases: List[Dict[str, Any]],
    samples: int = SAMPLES,
    judge_model: str = JUDGE_MODEL,
    effort: str = "",
    workers: int = 4,
    offline: bool = False,
) -> Dict[str, Any]:
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    agreement = None
    if judge_model:
        labels = json.loads((FIXTURES / "judge_labels.json").read_text(encoding="utf-8"))
        agreement = judge_agreement(client, judge_model, case_data.load(), labels)
    jobs = [(model, case, number) for model in models for case in cases for number in range(1, samples + 1)]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda job: run_sample(client, job[0], job[1], job[2], judge_model, effort), jobs))
    spent = [agreement["tokens"]] if agreement else []
    spent += [sample["grader_tokens"] for sample in results]
    return {
        "wording": case_data.wording(cases),
        "grader_tokens": {key: sum(item[key] for item in spent) for key in ("input", "output")},
        "started": started,
        "offline": offline,
        "models": list(models),
        "cases": [case["id"] for case in cases],
        "samples_per_case": samples,
        "judge_model": judge_model,
        "effort": effort,
        "rules": [{"id": rule.id, "says": rule.says} for rule in rules.RULES],
        "judge_agreement": agreement,
        "summary": summarise(results, models, cases),
        "results": results,
    }


def arguments(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals.wake", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", nargs="+", default=list(MODELS), metavar="ID", help="model ids to test (default: %(default)s)")
    parser.add_argument("--samples", type=int, default=SAMPLES, metavar="N", help="samples per case and model (default: %(default)s)")
    parser.add_argument("--cases", nargs="+", default=[], metavar="ID", help="run only these case ids")
    parser.add_argument("--judge-model", default=JUDGE_MODEL, metavar="ID", help="model for the neutrality grader (default: %(default)s)")
    parser.add_argument("--no-judge", action="store_true", help="skip the model grader and its agreement check")
    parser.add_argument(
        "--effort", choices=EFFORTS, default="", help="output_config.effort for the tested models (default: each model's own)"
    )
    parser.add_argument("--workers", type=int, default=4, metavar="N", help="samples run at once (default: %(default)s)")
    parser.add_argument("--out", type=Path, default=None, metavar="DIR", help="results folder (default: evals/results/<time>)")
    parser.add_argument("--offline", action="store_true", help="replay the written replies: no login, no network, no model")
    parser.add_argument(
        "--plugin-scripts",
        type=Path,
        default=None,
        metavar="DIR",
        help="render with the report.py of another checkout, such as ../emet-plugin-main/claude/scripts (default: this checkout)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None, client: Optional[Client] = None) -> int:
    options = arguments(argv)
    repeated = sorted({model for model in options.models if options.models.count(model) > 1})
    if repeated:
        raise SystemExit(f"Model listed twice: {', '.join(repeated)}")
    refuse_used(options.out)
    if options.plugin_scripts is not None:
        case_data.use_plugin(options.plugin_scripts)
    cases = case_data.load()
    if options.cases:
        unknown = sorted(set(options.cases) - {case["id"] for case in cases})
        if unknown:
            raise SystemExit(f"Unknown case: {', '.join(unknown)}")
        cases = [case for case in cases if case["id"] in options.cases]
    if client is None and options.offline:
        replies = json.loads((FIXTURES / "replies.json").read_text(encoding="utf-8"))
        labels = json.loads((FIXTURES / "judge_labels.json").read_text(encoding="utf-8"))
        client = ReplayClient(case_data.load(), replies, labels)
    if client is None:
        client = ClaudeCodeClient()
    models = ["replay"] if options.offline else options.models
    judge_model = "" if options.no_judge else "replay" if options.offline else options.judge_model
    result = run(client, models, cases, options.samples, judge_model, options.effort, options.workers, options.offline)
    result["plugin_scripts"] = str(options.plugin_scripts) if options.plugin_scripts is not None else ""
    folder = publish(result, options.out)
    sys.stdout.write(table(result))
    sys.stderr.write(f"\nResults and failing replies: {folder}\n")
    graded = sum(mine["samples"] - mine["errors"] for mine in result["summary"].values())
    return 0 if graded else 1


if __name__ == "__main__":
    raise SystemExit(main())
