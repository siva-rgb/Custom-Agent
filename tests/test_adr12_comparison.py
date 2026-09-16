"""M15 gate: the first ADR-12 comparison (FR-61, FR-62, FR-63, NFR-20, AC-48, AC-49, AC-50).

Written before the implementation, against the specification and the pre-flight recorded as
KNOWLEDGE-d94aee6c: the gateway serves the Anthropic Messages endpoint, and the Claude Agent SDK
runs against it through a locally pinned Claude Code CLI (DECISION-05ee16ad).

Everything here runs offline. AC-48 requires the whole task set to run with a scripted model on
this SDK's arm and a scripted stand-in for the other, so both arms take their runner as an
argument: no test in this file starts the CLI or reaches the gateway. The live comparison is a
command of its own, and AC-50 judges its report.

The harness lives in evals/adr12/, outside the package (FR-61), and these tests reach it through
evals.adr12, so the gate collects them from tests/ as FR-62 requires.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import pathlib
import subprocess
import sys
import tomllib
from decimal import Decimal

import pytest
from dotenv import dotenv_values

REPO = pathlib.Path(__file__).resolve().parents[1]
GATEWAY_MODEL = "bedrock.anthropic.claude-haiku-4-5"
CLI_PIN = "2.1.267"
FR61_TOOLS = ["Read", "Glob", "Grep"]
FR61_ENV = {
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
}
# FR-62: every measure a row carries, and what a missing one says instead of 0 (AC-50).
MEASURES = ("passed", "status", "turns", "prompt_tokens", "completion_tokens", "cache_read_tokens",
            "cache_write_tokens", "wall_ms", "cost_usd", "sdk_cost_estimate_usd")
UNAVAILABLE = "unavailable"


def tasks():
    return importlib.import_module("evals.adr12.tasks")


def arms():
    return importlib.import_module("evals.adr12.arms")


def harness():
    return importlib.import_module("evals.adr12.harness")


def prices():
    from agentsdk.registry import ModelPricing

    # One price list, used for both arms (AC-48).
    return ModelPricing(input="0.000001", output="0.000005", cache_read="0.0000001", cache_write="0.00000125")


def task_set():
    """The tasks, or an empty list before the harness exists, so collection still works."""
    try:
        return list(tasks().TASKS)
    except Exception:  # noqa: BLE001 - the module does not exist yet
        return []


@pytest.fixture
def workspace(tmp_path):
    """A run root outside the repository, as FR-61 requires of every arm."""
    root = tmp_path / "work"
    root.mkdir()
    assert REPO not in root.parents, "the run root must lie outside the repository"
    return root


# =================================================================================================
# FR-61: the task set and its fixture
# =================================================================================================


def test_the_task_set_is_twelve_tasks_over_the_fixture_with_deterministic_checkers():
    module = tasks()
    assert len(module.TASKS) == 12, [t.id for t in module.TASKS]
    assert len({t.id for t in module.TASKS}) == 12, "task ids repeat"
    assert set(module.KINDS) == {"single-step", "multi-step", "long", "decline"}
    assert {t.kind for t in module.TASKS} == set(module.KINDS)
    for task in module.TASKS:
        assert task.max_turns >= 1 and task.prompt.strip() and callable(task.checker)


def test_the_fixture_folder_ships_with_the_harness_and_holds_the_files_the_tasks_name():
    root = tasks().FIXTURE_ROOT
    assert root.is_dir() and root.parent == REPO / "evals" / "adr12"
    files = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    assert len(files) >= 5, files
    assert not any(p.is_symlink() for p in root.rglob("*")), "a fixture symlink would not survive copying"


def test_no_task_names_a_url_or_a_web_tool():
    """FR-61 leaves web tasks out; this keeps them out as the set grows."""
    for task in tasks().TASKS:
        lowered = task.prompt.lower()
        for forbidden in ("http://", "https://", "webfetch", "websearch", "localhost", "127.0.0.1"):
            assert forbidden not in lowered, (task.id, forbidden)


@pytest.mark.parametrize("task", task_set(), ids=lambda t: t.id)
def test_every_checker_passes_a_known_good_answer_and_fails_a_known_bad_one(task):
    assert task.checker(task.good_answer) is True, task.id
    assert task.checker(task.bad_answer) is False, task.id
    assert task.checker("") is False, f"{task.id}: an empty answer passed"
    assert task.checker(None) is False, f"{task.id}: a missing answer passed"
    if task.tricky_answer is not None:
        assert task.checker(task.tricky_answer) is False, f"{task.id}: a refused spelling was accepted"


def test_enough_checkers_are_pinned_against_an_answer_that_says_both():
    """Added after the mutation run (T2): without a tricky answer, a checker's refusal list is
    never exercised by a failing case, and dropping it changes nothing the gate can see."""
    with_tricky = [task.id for task in tasks().TASKS if task.tricky_answer is not None]
    assert len(with_tricky) >= 4, with_tricky


# =================================================================================================
# FR-61: the two arms
# =================================================================================================


def test_the_claude_arm_is_configured_exactly_as_fr61_says(workspace):
    arm = arms().ClaudeAgentSdkArm(
        cli_path=REPO / "evals" / "adr12" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe",
        model=GATEWAY_MODEL, base_url="https://gateway.test", auth_token="token-123", query_fn=lambda **_: None,
    )
    task = tasks().TASKS[0]
    options = arm.options_for(task, workspace)
    assert list(options.tools) == FR61_TOOLS
    assert list(options.allowed_tools) == FR61_TOOLS
    assert options.permission_mode == "dontAsk"
    assert options.setting_sources == []
    assert set(options.env) == FR61_ENV, sorted(options.env)
    assert options.env["ANTHROPIC_MODEL"] == GATEWAY_MODEL
    assert options.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == GATEWAY_MODEL
    assert options.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert options.model == GATEWAY_MODEL
    assert options.max_turns == task.max_turns
    assert pathlib.Path(options.cwd) == workspace
    assert pathlib.Path(options.cli_path).name == "claude.exe"


def test_the_claude_arm_names_the_pinned_cli_and_its_wire_format():
    pinned = json.loads((REPO / "evals" / "adr12" / "package.json").read_text(encoding="utf-8"))
    assert pinned["dependencies"]["@anthropic-ai/claude-code"] == CLI_PIN, "the CLI pin moved"
    arm = arms().ClaudeAgentSdkArm(cli_path="claude.exe", model=GATEWAY_MODEL, base_url="b", auth_token="t",
                                   query_fn=lambda **_: None)
    assert "claude-agent-sdk" in arm.version and CLI_PIN in arm.version, arm.version
    assert "Anthropic Messages" in arm.wire_format
    assert arm.name == "claude-agent-sdk"


def test_this_sdks_arm_names_its_version_and_wire_format_and_uses_the_m10_file_tools(workspace):
    from agentsdk.version import __version__

    arm = arms().ThisSdkArm(client_factory=lambda: None)
    assert __version__ in arm.version
    assert "OpenAI" in arm.wire_format
    assert arm.name == "agentsdk"
    assert sorted(tool.spec.name for tool in arm.tools_for(workspace)) == ["glob_files", "grep_files", "read_file"]


# =================================================================================================
# AC-48: the whole set runs offline, costed from one ModelPricing
# =================================================================================================


class ScriptedThisSdkClient:
    """Answers every task without a network: one tool call, then the answer its prompt asks for.

    Keyed by prompt rather than sequence: the harness runs repetition, then task, then arm, and a
    stand-in that answered in order would hand one task another task's answer.
    """

    def __init__(self, answers):
        self.answers = answers
        self.calls = 0

    async def send(self, request):
        from agentsdk.model import ModelResponse, StopReason, Usage
        from agentsdk.primitives import Message, Role, ToolCall

        self.calls += 1
        if not any(m.role is Role.TOOL for m in request.messages):
            call = ToolCall(id="c1", name="glob_files", arguments={"pattern": "**/*"})
            return ModelResponse(message=Message(role=Role.ASSISTANT, tool_calls=(call,)),
                                 stop_reason=StopReason.TOOL_CALLS, usage=Usage(100, 20, 120, 10, 5))
        asked = next(m.content for m in request.messages if m.role is Role.USER)
        return ModelResponse(message=Message(role=Role.ASSISTANT, content=self.answers[asked]),
                             stop_reason=StopReason.END_TURN, usage=Usage(100, 20, 120, 10, 5))


def scripted_claude(answers, usage=None):
    """A stand-in for the Claude Agent SDK's query(): one answer for the prompt, then one result."""
    from types import SimpleNamespace

    async def run(*, prompt, options=None, **kwargs):
        yield SimpleNamespace(kind="assistant", text=answers[prompt])
        yield SimpleNamespace(kind="result", num_turns=2, is_error=False, total_cost_usd=0.0042,
                              usage=usage or {"input_tokens": 90, "output_tokens": 30,
                                              "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5})

    return run


def offline_report(tmp_path, repetitions=1, answer_for=None):
    """Run the whole set offline. `answer_for(task)` decides what both arms answer."""
    task_module = tasks()
    chosen = answer_for or (lambda task: task.good_answer)
    answers = {task.prompt: chosen(task) for task in task_module.TASKS}
    this_arm = arms().ThisSdkArm(client_factory=lambda: ScriptedThisSdkClient(answers))
    claude_arm = arms().ClaudeAgentSdkArm(cli_path="claude.exe", model=GATEWAY_MODEL, base_url="https://gateway.test",
                                          auth_token="token-123", query_fn=scripted_claude(answers))
    return harness().run_comparison(
        arms=[this_arm, claude_arm], tasks=task_module.TASKS, repetitions=repetitions, pricing=prices(),
        reports_dir=tmp_path / "reports", workdir=tmp_path / "work", gateway_model=GATEWAY_MODEL,
    )


def test_the_whole_task_set_runs_offline_on_both_arms_and_writes_a_report(tmp_path):
    outcome = offline_report(tmp_path, repetitions=2)
    assert len(outcome.rows) == 12 * 2 * 2, len(outcome.rows)
    assert {row["arm"] for row in outcome.rows} == {"agentsdk", "claude-agent-sdk"}
    assert {row["repetition"] for row in outcome.rows} == {1, 2}
    failed = [row for row in outcome.rows if row["passed"] is not True]
    assert not failed, failed[:3]
    assert outcome.json_path.is_file() and outcome.markdown_path.is_file()
    assert outcome.json_path.parent == tmp_path / "reports"


def test_every_row_carries_every_fr62_measure_and_marks_a_missing_one_unavailable(tmp_path):
    outcome = offline_report(tmp_path)
    for row in outcome.rows:
        missing = [measure for measure in MEASURES if measure not in row]
        assert not missing, (row["task"], row["arm"], missing)
        for measure in MEASURES:
            assert row[measure] is not None, f"{measure} is None rather than unavailable"
    ours = [row for row in outcome.rows if row["arm"] == "agentsdk"]
    assert all(row["sdk_cost_estimate_usd"] == UNAVAILABLE for row in ours), "this SDK estimates no cost of its own"
    theirs = [row for row in outcome.rows if row["arm"] == "claude-agent-sdk"]
    assert all(row["sdk_cost_estimate_usd"] != UNAVAILABLE for row in theirs)


def test_both_arms_are_costed_from_the_same_model_pricing(tmp_path):
    from agentsdk.model import Usage
    from agentsdk.registry import call_cost

    outcome = offline_report(tmp_path)
    pricing = prices()
    for row in outcome.rows:
        usage = Usage(row["prompt_tokens"], row["completion_tokens"],
                      row["prompt_tokens"] + row["completion_tokens"],
                      row["cache_read_tokens"], row["cache_write_tokens"])
        assert Decimal(row["cost_usd"]) == call_cost(usage, pricing), (row["arm"], row["task"])
    # FR-62: the Claude arm's own figure is recorded separately, as it reported it. Live, the two
    # can agree, because the price list this comparison uses is the model's published rate; what
    # must hold is that the estimate is its own field, carrying its own value.
    claude = next(row for row in outcome.rows if row["arm"] == "claude-agent-sdk")
    assert claude["sdk_cost_estimate_usd"] == "0.0042", claude["sdk_cost_estimate_usd"]


def test_the_claude_arms_tokens_are_normalised_as_fr62_defines(tmp_path):
    """prompt = input + cache read + cache creation, so both arms count the same way."""
    outcome = offline_report(tmp_path)
    row = next(r for r in outcome.rows if r["arm"] == "claude-agent-sdk")
    assert row["prompt_tokens"] == 90 + 10 + 5, row
    assert row["completion_tokens"] == 30
    assert row["cache_read_tokens"] == 10 and row["cache_write_tokens"] == 5


def test_each_run_gets_its_own_copy_of_the_fixture_outside_the_repository(tmp_path):
    outcome = offline_report(tmp_path, repetitions=2)
    roots = {row["run_root"] for row in outcome.rows}
    assert len(roots) == len(outcome.rows), "runs shared a folder"
    for root in roots:
        path = pathlib.Path(root)
        assert REPO not in path.parents, path
        assert tmp_path in path.parents, path


def test_every_run_folder_holds_the_fixture(tmp_path):
    """Added after the mutation run (H5): the offline stand-ins answer without reading files, so
    nothing else notices a run folder that was never filled."""
    outcome = offline_report(tmp_path)
    expected = {p.relative_to(tasks().FIXTURE_ROOT).as_posix() for p in tasks().FIXTURE_ROOT.rglob("*") if p.is_file()}
    for row in outcome.rows:
        root = pathlib.Path(row["run_root"])
        assert {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} == expected, row["run_root"]
        assert (root / "README.md").read_text(encoding="utf-8") ==             (tasks().FIXTURE_ROOT / "README.md").read_text(encoding="utf-8")


def test_the_report_names_the_model_versions_wire_formats_and_date(tmp_path):
    from agentsdk.version import __version__

    outcome = offline_report(tmp_path)
    text = outcome.markdown_path.read_text(encoding="utf-8")
    written = json.loads(outcome.json_path.read_text(encoding="utf-8"))
    for needed in (GATEWAY_MODEL, __version__, CLI_PIN, "claude-agent-sdk", "Anthropic Messages", "OpenAI"):
        assert needed in text, needed
    assert written["metadata"]["date"] in text, "the report's Markdown does not carry the date"
    assert written["metadata"]["gateway_model"] == GATEWAY_MODEL
    assert len(written["metadata"]["date"]) >= 10 and written["metadata"]["date"].startswith("20")
    assert set(written["metadata"]["arms"]) == {"agentsdk", "claude-agent-sdk"}
    assert len(written["rows"]) == len(outcome.rows)


def test_the_report_reports_a_gap_as_measured_never_as_a_verdict(tmp_path):
    """FR-62: the report is an input to the next increment, not a pass or fail of this SDK."""
    text = offline_report(tmp_path).markdown_path.read_text(encoding="utf-8").lower()
    for verdict in ("this sdk wins", "claude loses", "better sdk", "verdict:"):
        assert verdict not in text, verdict


def test_a_failed_checker_is_recorded_as_a_failure_not_an_error(tmp_path):
    outcome = offline_report(tmp_path, answer_for=lambda task: "nonsense")
    assert all(row["passed"] is False for row in outcome.rows)
    assert all(row["status"] in ("completed", "max_turns_exceeded") for row in outcome.rows), \
        {row["status"] for row in outcome.rows}


# =================================================================================================
# NFR-20, AC-48: the comparison is never an SDK dependency
# =================================================================================================


def test_no_module_under_agentsdk_imports_the_claude_agent_sdk():
    offenders = []
    for path in (REPO / "agentsdk").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name.split(".")[0] == "claude_agent_sdk" for name in names):
                offenders.append(path.name)
    assert not offenders, offenders


def test_claude_agent_sdk_is_declared_only_in_the_eval_extra_and_the_harness_requirements():
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert "claude-agent-sdk==0.2.152" in project["optional-dependencies"]["eval"]
    assert not any(dep.lower().startswith("claude-agent-sdk") for dep in project["dependencies"])
    for extra, pins in project["optional-dependencies"].items():
        if extra != "eval":
            assert not any(pin.lower().startswith("claude-agent-sdk") for pin in pins), extra
    assert "claude-agent-sdk==0.2.152" in (REPO / "evals" / "adr12" / "requirements.txt").read_text(encoding="utf-8")
    assert "claude-agent-sdk" not in (REPO / "requirements.txt").read_text(encoding="utf-8"), \
        "the comparison reached the SDK's own requirements"


def test_the_harness_is_not_packaged_in_the_wheel():
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["tool"]["setuptools"]["packages"] == ["agentsdk", "agentsdk.providers"]


# =================================================================================================
# AC-50: where a live report lies, and what it must not carry
# =================================================================================================


def test_the_reports_folder_is_visible_to_the_credential_scan():
    reports = REPO / "evals" / "adr12" / "reports"
    assert reports.is_dir(), "reports/ must exist, so a live run has somewhere to write"
    probe = reports / "gitignore-probe.tmp"
    probe.write_text("probe\n", encoding="utf-8")
    try:
        listed = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", str(probe)],
                                cwd=REPO, capture_output=True, text=True, timeout=60).stdout
        assert probe.name in listed, "a file under reports/ is ignored, so AC-19's scan would not read it"
    finally:
        probe.unlink()


def test_a_written_report_carries_no_credential(tmp_path):
    values = dotenv_values(REPO / ".env")
    key = (values.get("MODEL_API_KEY") or "").strip()
    base = (values.get("BASE_URL") or "").strip()
    assert key and base, "MODEL_API_KEY and BASE_URL must be set in .env for this check"
    outcome = offline_report(tmp_path)
    for path in (outcome.json_path, outcome.markdown_path):
        text = path.read_text(encoding="utf-8")
        assert key not in text, f"{path.name} carries the model API key"
        assert base not in text, f"{path.name} carries the gateway host"


def test_the_live_entry_point_exists_and_refuses_to_run_without_configuration(tmp_path):
    """The live comparison is a command of its own; AC-48's tests never start it."""
    script = REPO / "evals" / "adr12" / "run_comparison.py"
    assert script.is_file()
    env = {k: v for k, v in os.environ.items() if k not in ("BASE_URL", "MODEL_API_KEY")}
    env.update(PYTHONPATH=str(REPO), PYTHONIOENCODING="utf-8")
    # Its reports go to a temporary folder: a mutant that skips the configuration check must not
    # be able to write into the repository reports folder (seen in the mutation run, E1).
    proc = subprocess.run([sys.executable, str(script), "--repetitions", "1", "--reports-dir", str(tmp_path)],
                          cwd=tmp_path, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode != 0
    assert "BASE_URL" in (proc.stdout + proc.stderr)
