"""M53 gate: the comparison's delegation tasks and how each arm's delegation is measured (FR-188,
FR-189, AC-111, NFR-53).

The pre-flight is DECISION-0a617b3f: six tasks, each split into independent pieces over its files,
checked by the workspace the parent left; this SDK's arm runs the delegating preset under ten times
M51's sandbox median, the Claude arm is given its CLI's Agent tool, and each row reports whether its
run delegated and how many children it ran -- never a condition of passing.

Offline, every checker is held against its good, bad and tricky outcomes, the code tasks through the
harness's verifier run on the host; this SDK's arm delegates on FakeProvider with a scripted model;
the Claude arm runs against the recording docker stand-in. The docker-marked tests run a delegating
run on DockerProvider and the verifier's container on the code tasks.
"""

from __future__ import annotations

import json
from dataclasses import replace
import statistics
import subprocess
from decimal import Decimal

import pytest

from agentsdk.delegation import DELEGATE_TOOL
from agentsdk.model import Usage
from agentsdk.presets import GENERAL_TOOLS, GENERAL_VERSION
from agentsdk.registry import ModelPricing
from agentsdk.workspace import WorkspaceSpec

from evals.adr12 import harness, run_comparison
from evals.adr12.delegation import DELEGATION_TASKS, DISPATCH, GROUP, TURNS
from evals.adr12.sandbox import (
    DELEGATION, FETCH_NAME, M51_REPORT, M51_SANDBOX_MEDIAN, SANDBOX, SANDBOX_TASKS, Workspace, applied, ceilings,
)
from evals.adr12.sandbox_arms import (
    CLAUDE_SANDBOX_TOOLS, CLAUDE_SUBAGENT_TOOL, PresetArm, SandboxOutcome, Verifier, subagents,
)
from evals.adr12.tasks import TASKS
from agentsdk.primitives import ToolCall
from tests.test_adr12_sandbox import (
    FAKE_IMAGE, HERE, MODEL, WORKSPACE_IMAGE, CountingProvider, LocalVerifier, RecordingDocker, Scripted, _claude,
    _docker_ready, _good_turns, _registry, _respond, _verify_here,
)

BY_ID = {task.id: task for task in DELEGATION_TASKS}
CODE = [task for task in DELEGATION_TASKS if task.verification]
CALL = Usage(50, 10, 60)  # what each scripted call spends


# =================================================================================================
# FR-188: the task set
# =================================================================================================


def test_the_delegation_kind_is_six_tasks_beside_the_thirty_six():
    assert [t.id for t in DELEGATION_TASKS] == ["word-counts", "csv-totals", "log-errors", "ini-to-json",
                                                 "three-functions", "fix-three-bugs"]
    ids = {t.id for t in DELEGATION_TASKS}
    assert not ids & ({t.id for t in SANDBOX_TASKS} | {t.id for t in TASKS})
    assert len(SANDBOX_TASKS) == 24 and len(TASKS) == 12, "the earlier tasks stay"
    for task in DELEGATION_TASKS:
        assert (task.kind, task.group, task.max_turns, task.egress) == (DELEGATION, GROUP, TURNS, ())
        assert task.prompt.strip() and callable(task.checker)
        assert len(task.files) >= 3, f"{task.id} does not split into three or more pieces over its files"
    assert len(CODE) == 2 and all(t.verification_files == {"dispatch.py": DISPATCH} for t in CODE)


def judged(task, which, tmp_path) -> Workspace:
    space = task.outcome(which)
    verified = _verify_here(task, dict(space.files), tmp_path) if task.verification else None
    return task.outcome(which, verified=verified)


@pytest.mark.parametrize("task", DELEGATION_TASKS, ids=lambda t: t.id)
def test_every_checker_accepts_its_good_outcome_and_refuses_its_bad_and_tricky(task, tmp_path):
    """AC-111: the three outcomes, a run that left nothing, and one that only answered."""
    assert task.checker(judged(task, "good", tmp_path)) is True
    assert task.checker(judged(task, "bad", tmp_path)) is False
    assert task.checker(judged(task, "tricky", tmp_path)) is False
    assert task.checker(Workspace(files={})) is False
    assert task.checker(Workspace(files=dict(task.files), answer="done, every piece")) is False
    assert task.checker(None) is False


@pytest.mark.parametrize("task", DELEGATION_TASKS, ids=lambda t: t.id)
def test_each_outcome_differs_from_the_others(task):
    good, bad, tricky = (applied(task.files, c) for c in (task.good, task.bad, task.tricky))
    assert good != bad and good != tricky and bad != tricky


@pytest.mark.parametrize("task", DELEGATION_TASKS, ids=lambda t: t.id)
def test_a_changed_input_fails_even_beside_the_right_result(task, tmp_path):
    """The inputs come back as they were: the good result over an edited input fails."""
    first = sorted(task.files)[0]  # an input every task keeps: a chapter, a region, a log, a config, a spec, a check
    space = applied(task.files, {**task.good, first: task.files[first] + b"x"})
    verified = _verify_here(task, space, tmp_path) if task.verification else None
    assert task.checker(Workspace(files=space, verified=verified)) is False, first


@pytest.mark.parametrize("task", CODE, ids=lambda t: t.id)
def test_a_code_tasks_verification_covers_every_module(task, tmp_path):
    """The dispatcher calls each module by its path: each module wrong alone fails the verification."""
    modules = sorted({case[0][0] for case in task.verification[2]})
    assert len(modules) == 3
    good = applied(task.files, task.good)
    assert _verify_here(task, good, tmp_path) == "pass"
    for module in modules:
        broken = {**good, f"{module}.py": b"def nothing():\n    pass\n"}
        assert _verify_here(task, broken, tmp_path) != "pass", module


def test_each_data_tasks_expected_result_is_its_inputs_read_independently():
    """The checkers' expected results, recomputed from the inputs by the standard library: a wrong
    expectation would pass a wrong run and fail a right one (M53 mutation run)."""
    import configparser
    import csv
    import io
    from decimal import Decimal as D

    def files(task_id):
        return {path: data.decode() for path, data in BY_ID[task_id].files.items()}

    words = {path: len(text.split()) for path, text in files("word-counts").items()}
    assert json.loads(BY_ID["word-counts"].good["counts.json"]) == words
    totals = sorted((path.split("/")[1][:-4], sum(D(row["amount"].replace(",", "")) for row in
                                                     csv.DictReader(io.StringIO(text))))
                    for path, text in files("csv-totals").items())
    assert BY_ID["csv-totals"].good["totals.csv"].decode() == "region,total\n" + "".join(
        f"{region},{total:.2f}\n" for region, total in totals)
    errors = {path.split("/")[1][:-4]: sum(1 for line in text.splitlines() if line.split()[2] == "ERROR")
              for path, text in files("log-errors").items()}
    assert json.loads(BY_ID["log-errors"].good["errors.json"]) == errors
    for path, text in files("ini-to-json").items():
        parser = configparser.ConfigParser(inline_comment_prefixes=(";",), strict=False, interpolation=None)
        parser.read_string(text)
        read = {section: dict(parser[section]) for section in parser.sections()}
        assert json.loads(BY_ID["ini-to-json"].good[path[:-4] + ".json"]) == read, path


def test_fix_three_bugs_is_verified_on_hidden_cases_too(tmp_path):
    """A fix that passes every visible check but not a hidden case fails verification."""
    from evals.adr12.delegation import _BUGGY

    task = BY_ID["fix-three-bugs"]
    narrow = (b"def to_order(stock, target, minimum):\n"
              b"    return {item: target - count for item, count in stock.items() if count <= minimum and count < 5}\n")
    scope: dict = {}
    exec(narrow, scope)  # noqa: S102 - the test's own source
    _, _, _, visible, hidden = _BUGGY["inventory"]
    assert all(scope["to_order"](*arguments) == expected for arguments, expected in visible), "passes the check"
    assert not all(scope["to_order"](*arguments) == expected for arguments, expected in hidden)
    good = applied(task.files, task.good)
    assert _verify_here(task, good, tmp_path / "good") == "pass"
    assert _verify_here(task, {**good, "inventory.py": narrow}, tmp_path / "narrow") != "pass"


@pytest.mark.parametrize("task_id, path, key, spelled", [
    ("word-counts", "counts.json", "chapters/two.txt", False), ("log-errors", "errors.json", "web", False),
    ("word-counts", "counts.json", "chapters/one.txt", 7.0), ("log-errors", "errors.json", "worker", 3.0)])
def test_a_json_result_is_compared_type_for_type(task_id, path, key, spelled):
    """M53 pre-review F4: Python's == takes false for 0 and 7.0 for 7; the checker does not."""
    task = BY_ID[task_id]
    right = json.loads(task.good[path])
    wrong = {**right, key: spelled}
    assert wrong == right, "the two are equal to Python, so == alone would pass this"
    assert task.checker(Workspace(files=applied(task.files, task.good))) is True
    assert task.checker(Workspace(files=applied(task.files, {path: json.dumps(wrong).encode()}))) is False


def test_a_code_tasks_module_may_import_a_sibling_of_the_run(tmp_path):
    """M53 pre-review F5: the dispatcher loads each module from the run's folder, and that folder is
    on the import path after the standard library, as when the module runs there."""
    task = BY_ID["three-functions"]
    good = applied(task.files, task.good)
    split = {**good, "helper.py": task.good["slugify.py"],
             "slugify.py": b"from helper import slugify as _slugify\n\n\ndef slugify(title):\n    return _slugify(title)\n"}
    assert _verify_here(task, split, tmp_path / "split") == "pass"
    # colorsys: a standard module nothing loads before the dispatcher, so the import path decides it
    # (json, loaded by the verifier first, would prove nothing -- M53 mutation run).
    uses = {**good, "roman.py": b"import colorsys\n" + task.good["roman.py"]}
    assert _verify_here(task, uses, tmp_path / "uses") == "pass"
    shadow = {**uses, "colorsys.py": b"raise SystemExit('shadowed')\n"}
    assert _verify_here(task, shadow, tmp_path / "shadow") == "pass", "a run's colorsys.py does not shadow the stdlib"


_SANDBOX_BY_ID = {task.id: task for task in SANDBOX_TASKS}


@pytest.mark.parametrize("task_id, path, wrapper", [
    ("isbn10", "isbn.py", b"\n_valid = valid\n\n\ndef valid(code):\n    return int(_valid(code))\n"),
    ("roman-numerals", "roman.py", b"\n_to_int = to_int\n\n\ndef to_int(text):\n    return float(_to_int(text))\n"),
    ("three-functions", "roman.py", b"\n_to_int = to_int\n\n\ndef to_int(text):\n    return float(_to_int(text))\n"),
])
def test_the_verifier_compares_each_result_type_for_type(task_id, path, wrapper, tmp_path):
    """M53 round 1, the sweep after F4: the verifier compared results with Python's ==, which takes 1
    for True and 7.0 for 7; directly and through the dispatcher, it now compares them as JSON."""
    task = BY_ID.get(task_id) or _SANDBOX_BY_ID[task_id]
    good = applied(task.files, task.good)
    assert _verify_here(task, good, tmp_path / "good") == "pass"
    assert 1 == True and 7.0 == 7, "the two are equal to Python, so == alone would pass this"  # noqa: E712
    assert _verify_here(task, {**good, path: good[path] + wrapper}, tmp_path / "loose") != "pass"


def test_the_site_checker_compares_its_byte_count_type_for_type():
    """The same sweep: site.json's bytes written as 1256.0 is not the count 1256."""
    task = _SANDBOX_BY_ID["fetch-title-into-config"]
    page = b"<html><head><title>Example Domain</title></head><body>x</body></html>"
    site = {**json.loads(task.files["site.json"]), "title": "Example Domain", "bytes": len(page)}
    for value, expected in ((len(page), True), (float(len(page)), False)):
        files = applied(task.files, {"site.json": json.dumps({**site, "bytes": value}).encode()})
        assert task.checker(Workspace(files=files, witness=page, fetched=frozenset({FETCH_NAME}))) is expected, value


@pytest.mark.parametrize("task_id", ["word-counts", "log-errors", "add-license-field", "fetch-title-into-config"])
def test_a_json_checker_fails_a_file_nested_too_deep_to_read_rather_than_raising(task_id):
    """M53 round 1 NB-4: json.loads raises RecursionError, not ValueError, on deep nesting; a checker
    that let it out would end the comparison instead of failing the row."""
    task = BY_ID.get(task_id) or _SANDBOX_BY_ID[task_id]
    deep = {path: b"[" * 100_000 for path, data in task.files.items() if path.endswith(".json")}
    deep = deep or {next(p for p in task.good if p.endswith(".json")): b"[" * 100_000}
    space = Workspace(files=applied(task.files, {**task.good, **deep}), witness=b"x", fetched=frozenset({FETCH_NAME}))
    assert task.checker(space) is False


def test_each_tuning_set_logs_in_a_folder_of_its_own(monkeypatch, tmp_path):
    """M53 pre-review F1: --set delegation without --directory logs beside M51's, not into it."""
    from evals.adr12 import tune

    assert tune.SET_DIRECTORIES == {"sandbox": tune.TUNING_DIR, "delegation": tune.TUNING_DIR / "delegation"}
    seen = []

    def fake_tune(**kwargs):
        seen.append((kwargs["directory"], [t.id for t in kwargs["tasks"]], kwargs["witness"]))
        return {"version": "v", "passed": [], "failed": [], "cost_usd": "0"}

    monkeypatch.setattr(run_comparison, "credentials", lambda purpose: ("http://gateway.invalid", "key"))
    monkeypatch.setattr(tune, "tune", fake_tune)
    assert tune.main(["--model", tune.MINI, "--set", "delegation"]) == 0
    assert tune.main(["--model", tune.MINI, "--set", "delegation", "--directory", str(tmp_path)]) == 0
    assert seen == [(tune.TUNING_DIR / "delegation", [t.id for t in DELEGATION_TASKS], None),
                    (tmp_path, [t.id for t in DELEGATION_TASKS], None)]
    for directory, count in ((tune.TUNING_DIR, 24), (tune.TUNING_DIR / "delegation", 6)):
        log = tune.read_log(directory)
        assert log and all(len(item["passed"]) + len(item["failed"]) == count for item in log), directory
        assert (directory / "tuning-log.md").read_text(encoding="utf-8") == tune.markdown(log), directory


@pytest.mark.parametrize("module", ["stats", "dates", "inventory"])
def test_each_given_check_writes_its_own_check_file_when_its_module_is_fixed(module, tmp_path):
    """check_<module>.py, run as the prompt says, writes check_<module>.txt -- the file the good
    outcome holds -- and nothing on the buggy module."""
    import sys

    task = BY_ID["fix-three-bugs"]
    for name, data in task.files.items():
        (tmp_path / name).write_bytes(data)
    # -B: the fix is the buggy file's size, written within its second, so a cached .pyc would pass as fresh.
    check = [sys.executable, "-B", f"check_{module}.py"]
    subprocess.run(check, cwd=tmp_path, capture_output=True, timeout=60)
    assert not (tmp_path / f"check_{module}.txt").exists(), "the given module is buggy"
    (tmp_path / f"{module}.py").write_bytes(task.good[f"{module}.py"])
    done = subprocess.run(check, cwd=tmp_path, capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr
    # On this host Python writes text with CRLF; the workspace container, LF.
    written = (tmp_path / f"check_{module}.txt").read_bytes()
    assert written.replace(b"\r\n", b"\n") == task.good[f"check_{module}.txt"]
    assert not (tmp_path / "check.txt").exists()


@pytest.mark.parametrize("task", DELEGATION_TASKS, ids=lambda t: t.id)
def test_every_tasks_files_are_a_workspace_the_preset_accepts(task):
    space = WorkspaceSpec(image=FAKE_IMAGE, files=dict(task.files))
    assert dict(space.files) == dict(task.files)


def test_the_budget_rule_is_ten_times_m51s_sandbox_median_recomputed_from_that_report():
    data = json.loads((HERE / "reports" / M51_REPORT).read_text(encoding="utf-8"))
    rows = [r for r in data["rows"] if r["arm"] == "agentsdk" and r["kind"] == SANDBOX]
    assert len(rows) == 72 and data["metadata"]["preset_version"] == "general-v4"
    usd = statistics.median(Decimal(r["cost_usd"]) for r in rows)
    tokens = statistics.median(r["prompt_tokens"] + r["completion_tokens"] for r in rows)
    assert (Decimal(M51_SANDBOX_MEDIAN[0]), M51_SANDBOX_MEDIAN[1]) == (usd, tokens)
    assert ceilings(DELEGATION) == (str(usd * 10), int(tokens * 10)) == ("0.203690", 169050)
    assert ceilings(SANDBOX) == ("0.023940", 20175), "the sandbox kind's rule is unchanged"


# =================================================================================================
# This SDK's arm: the delegating preset, offline on FakeProvider
# =================================================================================================


class Delegating:
    """The parent hands `pieces` named files to children in one turn, then writes the good outcome
    and answers. A child -- a run with no delegate_task -- counts its file's words with a command of
    its own, then answers. Counts every call, parent and child."""

    def __init__(self, task, pieces=1):
        self.task, self.pieces, self.calls = task, pieces, 0
        self.writes = _good_turns(task)[:-1]  # the good outcome's writes, without the export

    async def send(self, request):
        self.calls += 1
        turn = sum(1 for m in request.messages if m.role.value == "assistant")
        if DELEGATE_TOOL not in {tool["function"]["name"] for tool in request.tools}:
            task = next(m.content for m in request.messages if m.role.value == "user")
            path = task.split()[1]
            return _respond(calls=[ToolCall(id="c0", name="run_command", arguments={"argv": ["wc", "-w", path]})]
                            ) if turn == 0 else _respond("piece done")
        if turn == 0:
            named = sorted(self.task.files)[:self.pieces]
            return _respond(calls=[ToolCall(id=f"d{i}", name=DELEGATE_TOOL,
                                            arguments={"objective": f"count {path}", "files": [path]})
                                   for i, path in enumerate(named)])
        if turn - 1 < len(self.writes):
            return _respond(calls=[ToolCall(id=f"t{turn}-{i}", name=name, arguments=args)
                                   for i, (name, args) in enumerate(self.writes[turn - 1])])
        return _respond("done")


def _arm(model, provider=None):
    provider = provider or CountingProvider()
    return PresetArm(client_factory=lambda: model, model=MODEL, registry=_registry(), image=FAKE_IMAGE,
                     provider=provider), provider


def test_this_sdks_arm_runs_the_delegating_preset_on_delegation_tasks_alone():
    """FR-188 on this arm, and NFR-53 for the harness: a sandbox task's agent is as it was."""
    arm, _ = _arm(Scripted([]))
    agent = arm.agent_for(BY_ID["word-counts"])
    assert agent.spec.preset == ("general", GENERAL_VERSION)
    assert agent.spec.tool_profile == (*GENERAL_TOOLS, DELEGATE_TOOL) and agent.spec.delegate is not None
    assert (agent.budget.run_ceiling_usd, agent.budget.run_ceiling_tokens) == (Decimal("0.203690"), 169050)
    assert agent.max_turns == TURNS and agent.spec.workspace.keep is True
    plain = arm.agent_for(SANDBOX_TASKS[0])
    assert plain.spec.tool_profile == GENERAL_TOOLS and plain.spec.delegate is None


@pytest.mark.parametrize("pieces", [1, 2])
async def test_this_sdks_arm_counts_the_children_and_the_whole_runs_usage(pieces, tmp_path):
    task = BY_ID["word-counts"]
    model = Delegating(task, pieces)
    arm, provider = _arm(model)
    outcome = await arm.run_sandbox(task)
    assert outcome.status == "completed", outcome.error
    assert outcome.children == pieces
    assert task.checker(Workspace(files=dict(outcome.files))) is True
    # Every call, the children's included: the parent's alone would be pieces fewer.
    assert outcome.usage == Usage(*(n * model.calls for n in (CALL.prompt_tokens, CALL.completion_tokens,
                                                               CALL.total_tokens)))
    assert model.calls > outcome.turns, "no child's call was counted"
    assert await provider.list_owned({}) == [], "a workspace remains"


async def test_a_run_that_delegates_nothing_reports_zero_children_and_still_passes():
    """Delegation is never a condition of passing (P5A-D21)."""
    task = BY_ID["word-counts"]
    arm, _ = _arm(Scripted(_good_turns(task)))
    outcome = await arm.run_sandbox(task)
    assert outcome.status == "completed", outcome.error
    assert outcome.children == 0
    assert task.checker(Workspace(files=dict(outcome.files))) is True


async def test_a_sandbox_tasks_usage_is_the_runs_own_as_before():
    """NFR-53 for the harness: no counting client on a sandbox task."""
    task = SANDBOX_TASKS[0]
    model = Scripted(_good_turns(task))
    arm, _ = _arm(model)
    outcome = await arm.run_sandbox(task)
    assert outcome.usage.total_tokens == CALL.total_tokens * outcome.turns
    assert outcome.children is None, "a run given nothing to delegate with reports no delegation measure"


# =================================================================================================
# The Claude arm: its CLI's Agent tool
# =================================================================================================


async def test_the_claude_arm_is_given_its_agent_tool_on_delegation_tasks_alone():
    docker = RecordingDocker()
    await _claude(docker).run_sandbox(BY_ID["csv-totals"])
    request = json.loads(docker.calls[2]["stdin"])
    assert request["tools"] == [*CLAUDE_SANDBOX_TOOLS, CLAUDE_SUBAGENT_TOOL] == [
        "Read", "Glob", "Grep", "Bash", "Write", "Edit", "Agent"]
    docker = RecordingDocker()
    await _claude(docker).run_sandbox(SANDBOX_TASKS[0])
    assert json.loads(docker.calls[2]["stdin"])["tools"] == list(CLAUDE_SANDBOX_TOOLS)


def test_the_claude_arms_children_are_its_agent_calls_whose_result_was_not_an_error():
    events = [
        {"kind": "assistant", "text": "", "tools": [{"id": "a1", "name": "Agent", "input": {}},
                                                   {"id": "a2", "name": "Agent", "input": {}},
                                                   {"id": "b1", "name": "Bash", "input": {}}]},
        {"kind": "results", "results": [{"id": "a1", "is_error": False}, {"id": "a2", "is_error": True},
                                        {"id": "b1", "is_error": False}]},
        {"kind": "assistant", "text": "", "tools": [{"id": "a3", "name": "Agent", "input": {}}]},
    ]
    assert subagents(events) == 1, "an errored call, another tool and a call with no result are no child"
    assert subagents([*events, {"kind": "results", "results": [{"id": "a3", "is_error": False}]}]) == 2
    assert subagents([]) == 0
    again = {"kind": "results", "results": [{"id": "a1", "is_error": False}]}
    assert subagents([*events, again, again]) == 1, "a result seen twice is one child"


async def test_the_claude_arm_reports_its_children_from_its_transcript():
    lines = [{"kind": "assistant", "text": "", "tools": [{"id": "a1", "name": "Agent", "input": {}}]},
             {"kind": "results", "results": [{"id": "a1", "is_error": False}]},
             {"kind": "assistant", "text": "done"},
             {"kind": "result", "num_turns": 2, "usage": {"input_tokens": 10, "output_tokens": 2},
              "total_cost_usd": 0.0001, "subtype": "success", "is_error": False}]
    outcome = await _claude(RecordingDocker(driver_lines=lines)).run_sandbox(BY_ID["log-errors"])
    assert outcome.children == 1
    outcome = await _claude(RecordingDocker(driver_lines=lines)).run_sandbox(SANDBOX_TASKS[0])
    assert outcome.children is None, "a sandbox task is given no Agent tool, so reports no measure"


@pytest.mark.parametrize("calls, partial", [
    ([{"id": "a1", "is_error": False}], True), ([{"id": "a1", "is_error": True}], True), (None, False)])
async def test_the_claude_arms_usage_is_partial_once_it_called_its_agent_tool(calls, partial):
    """M53 round 1 NB-1: the CLI's usage leaves out its subagents' (9.7x under on a forced run), so a
    run that called Agent at all -- an errored call may have spent too -- reports its usage as partial."""
    lines = [{"kind": "assistant", "text": "done"},
             {"kind": "result", "num_turns": 2, "usage": {"input_tokens": 10, "output_tokens": 2},
              "total_cost_usd": 0.0527849, "subtype": "success", "is_error": False}]
    if calls is not None:
        lines = [{"kind": "assistant", "text": "", "tools": [{"id": "a1", "name": "Agent", "input": {}}]},
                 {"kind": "results", "results": calls}, *lines]
    outcome = await _claude(RecordingDocker(driver_lines=lines)).run_sandbox(BY_ID["log-errors"])
    assert outcome.usage_partial is partial
    assert outcome.sdk_cost_estimate == Decimal("0.0527849")


def test_a_row_whose_usage_is_partial_reports_the_estimate_and_no_tokens(tmp_path):
    """M53 round 1 NB-1: such a row's cost is the CLI's own total, which counts the subagents, and its
    tokens are unavailable; a total over an arm's runs is unavailable when any run's is (as children)."""
    tasks = [BY_ID["word-counts"], BY_ID["log-errors"], BY_ID["three-functions"]]

    class Partial(StandInDelegatingClaude):
        async def run_sandbox(self, task):
            outcome = await super().run_sandbox(task)
            if task.id == "word-counts":
                return replace(outcome, usage_partial=True, sdk_cost_estimate=Decimal("0.0527849"))
            if task.id == "log-errors":
                return replace(outcome, usage_partial=True, sdk_cost_estimate=None)
            return replace(outcome, sdk_cost_estimate=Decimal("0.0001"))

    comparison = harness.run_comparison(
        arms=[], tasks=[], repetitions=1, pricing=ModelPricing(input="0.000001", output="0.000005"),
        reports_dir=tmp_path / "r", workdir=tmp_path / "w", gateway_model="g", sandbox_arms=[Partial()],
        sandbox_tasks=tasks, verifier=LocalVerifier(tmp_path))
    first, second, third = comparison.rows
    keys = ("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens")
    assert [first[k] for k in keys] == [second[k] for k in keys] == ["unavailable"] * 4
    assert (first["cost_usd"], second["cost_usd"]) == ("0.0527849", "unavailable"), "an unknown cost is never 0"
    assert third["prompt_tokens"] == 10 and third["cost_usd"] == "0.000020", "a whole usage is priced as before"
    assert "subagents" in harness.DELEGATION_MEASURE and "total_cost_usd" in harness.DELEGATION_MEASURE
    text = comparison.markdown_path.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| delegation | claude-agent-sdk |"))
    assert row.count("unavailable") >= 2, row


# =================================================================================================
# The harness and the report: each row says whether its run delegated
# =================================================================================================


class StandInDelegatingClaude:
    name = "claude-agent-sdk"
    wire_format = "Anthropic Messages through the Claude Code CLI"
    version = "stand-in"

    async def run_sandbox(self, task):
        return SandboxOutcome(answer="x", status="completed", turns=2, usage=Usage(10, 2, 12), wall_ms=1.0,
                              files=applied(task.files, task.good), children=3)


def test_the_harness_reports_whether_each_run_delegated_and_its_children(tmp_path):
    tasks = [BY_ID["word-counts"], BY_ID["three-functions"]]

    class Ours(PresetArm):
        def __init__(self):
            super().__init__(client_factory=lambda: next(self._next), model=MODEL, registry=_registry(),
                             image=FAKE_IMAGE, provider=CountingProvider())
            self._next = iter([Delegating(tasks[0]), Scripted(_good_turns(tasks[1]))])

    comparison = harness.run_comparison(
        arms=[], tasks=[], repetitions=1, pricing=ModelPricing(input="0.000001", output="0.000005"),
        reports_dir=tmp_path / "reports", workdir=tmp_path / "work", gateway_model="g",
        sandbox_arms=[Ours(), StandInDelegatingClaude()], sandbox_tasks=tasks, stamp="2026-11-10T00:00:00+00:00",
        verifier=LocalVerifier(tmp_path))
    rows = {(r["task"], r["arm"]): r for r in comparison.rows}
    assert [(r["task"], r["arm"], r["passed"], r["delegated"], r["children"]) for r in comparison.rows] == [
        ("word-counts", "agentsdk", True, True, 1), ("word-counts", "claude-agent-sdk", True, True, 3),
        ("three-functions", "agentsdk", True, False, 0), ("three-functions", "claude-agent-sdk", True, True, 3)]
    assert rows[("word-counts", "agentsdk")]["budget_usd"] == "0.203690"
    meta = comparison.metadata
    assert meta["kinds"] == {"delegation": 2} and meta["ceilings"]["delegation"] == {"usd": "0.203690",
                                                                                     "tokens": 169050}
    assert meta["setups"]["delegation"] == harness.DELEGATION_SETUP and meta["delegation"] == harness.DELEGATION_MEASURE
    assert "M51" in meta["budget_rule"] and M51_REPORT in meta["budget_rule"]
    text = comparison.markdown_path.read_text(encoding="utf-8")
    for needle in ("## Delegation", harness.DELEGATION_MEASURE, "| agentsdk | 2 | 1 | 1 |",
                   "| claude-agent-sdk | 2 | 2 | 6 |", "| word-counts | agentsdk | 1 | yes | yes | 1 |"):
        assert needle in text, needle


def test_an_arms_children_in_all_is_unavailable_when_any_run_was_not_read(tmp_path):
    """A total over runs some of which were not counted is no total."""
    tasks = [BY_ID["word-counts"], BY_ID["log-errors"]]

    class Unread(StandInDelegatingClaude):
        async def run_sandbox(self, task):
            outcome = await super().run_sandbox(task)
            return replace(outcome, children=None) if task.id == "log-errors" else outcome

    comparison = harness.run_comparison(
        arms=[], tasks=[], repetitions=1, pricing=ModelPricing(input="0", output="0"), reports_dir=tmp_path / "r",
        workdir=tmp_path / "w", gateway_model="g", sandbox_arms=[Unread()], sandbox_tasks=tasks)
    assert [r["children"] for r in comparison.rows] == [3, "unavailable"]
    text = comparison.markdown_path.read_text(encoding="utf-8")
    assert "| claude-agent-sdk | 2 | 1 | unavailable |" in text
    assert "| claude-agent-sdk | 2 | 1 | 3 |" not in text


def test_a_sandbox_only_comparison_has_no_delegation_section(tmp_path):
    """NFR-53 for the report: a comparison without the kind reads as it did."""
    from tests.test_adr12_sandbox import StandInClaude

    task = SANDBOX_TASKS[0]
    comparison = harness.run_comparison(
        arms=[], tasks=[], repetitions=1, pricing=ModelPricing(input="0", output="0"), reports_dir=tmp_path / "r",
        workdir=tmp_path / "w", gateway_model="g", sandbox_arms=[StandInClaude()], sandbox_tasks=[task])
    assert comparison.metadata["delegation"] is None
    assert "## Delegation" not in comparison.markdown_path.read_text(encoding="utf-8")
    assert "children" not in comparison.rows[0] and "delegated" not in comparison.rows[0], \
        "a sandbox row reads as before (M53 pre-review F3)"


def test_the_live_command_runs_the_delegation_set_and_every_task_in_all():
    tasks, spaces = run_comparison.chosen_tasks("delegation")
    assert tasks == [] and spaces == list(DELEGATION_TASKS)
    tasks, spaces = run_comparison.chosen_tasks("all")
    assert len(tasks) == 12 and spaces == [*SANDBOX_TASKS, *DELEGATION_TASKS], "FR-189: every task"
    assert run_comparison.chosen_tasks("sandbox")[1] == list(SANDBOX_TASKS)
    assert run_comparison.chosen_tasks("all", "csv-totals") == ([], [BY_ID["csv-totals"]])
    assert "delegation" in run_comparison.SETS


# =================================================================================================
# Docker: a delegating run on DockerProvider, and the verifier's container on the code tasks
# =================================================================================================


@pytest.mark.docker
async def test_this_sdks_arm_on_docker_runs_a_child_in_a_workspace_of_its_own():
    assert _docker_ready() is None, _docker_ready()
    from agentsdk.workspace.docker import DockerProvider

    task = BY_ID["word-counts"]
    provider = DockerProvider()
    arm = PresetArm(client_factory=lambda: Delegating(task), model=MODEL, registry=_registry(),
                    image=WORKSPACE_IMAGE, provider=provider)
    outcome = await arm.run_sandbox(task)
    assert outcome.status == "completed", outcome.error
    assert outcome.children == 1
    assert task.checker(Workspace(files=dict(outcome.files))) is True
    assert await provider.list_owned({}) == [], "a workspace remains"


@pytest.mark.docker
async def test_the_verifier_container_passes_each_code_tasks_good_outcome_and_refuses_its_tricky():
    assert _docker_ready() is None, _docker_ready()
    verifier = Verifier(image=WORKSPACE_IMAGE)
    for task in CODE:
        assert await verifier.verify(task, applied(task.files, task.good)) == "pass", task.id
        assert await verifier.verify(task, applied(task.files, task.tricky)) != "pass", task.id
    left = subprocess.run(["docker", "ps", "-a", "--filter", "label=adr12.verifier=1", "--format", "{{.Names}}"],
                          capture_output=True, text=True, timeout=60)
    assert left.stdout.strip() == "", f"verifier containers left: {left.stdout}"
