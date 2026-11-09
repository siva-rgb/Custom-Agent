"""M50 gate: the larger comparison's sandbox tasks and their two arms (FR-179, FR-180, AC-108, NFR-51).

The pre-flight is DECISION-d0269633: the 12 M15 tasks keep their setup, the 24 sandbox tasks run
on the general preset and in the Claude arm's container, the fetch tasks allow example.com:443,
the Claude image is built FROM the workspace image's digest by the owner, and the budget rule is
FR-179's as written.

Offline, every sandbox checker is held against its good, bad and tricky outcomes, and each task's
own check.py is run for real on the good and the given code; this SDK's arm runs on FakeProvider
with a scripted model; the Claude arm runs against a recording stand-in for the docker client.
The docker-marked tests run this SDK's arm on DockerProvider and the Claude arm's container
plumbing on the locally built image, which no test pulls or builds.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import io
import json
import os
import pathlib
import statistics
import subprocess
import sys
import tarfile
from decimal import Decimal

import pytest

from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.presets import GENERAL_TOOLS, GENERAL_VERSION
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry
from agentsdk.workspace import FakeProvider, WorkspaceSpec

from evals.adr12 import claude_driver, harness, report
from evals.adr12.sandbox import (
    FETCH_HOST, FETCH_NAME, GROUPS, M15_MEDIANS, M15_REPORT, SANDBOX, SANDBOX_TASKS, VERIFIER, SandboxTask, Workspace, _PAGE,
    applied, ceilings, verification_input, verification_result,
)
from evals.adr12.sandbox_arms import (
    APPROVER, CLAUDE_IMAGE, CLAUDE_KEY_VARIABLE, CLAUDE_LABEL, CLAUDE_SANDBOX_TOOLS, CLAUDE_UID, PROJECT, TENANT,
    ClaudeContainerArm, DockerCli, PresetArm, SandboxOutcome, Verifier, fetch_witness, files_into_work, read_back,
)
from agentsdk.workspace.docker import MAX_TRANSFER_BYTES
from evals.adr12.tasks import TASKS

REPO = pathlib.Path(__file__).resolve().parents[1]
HERE = REPO / "evals" / "adr12"
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
WORKSPACE_IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
MODEL = "scripted"
KEY = "sk-test-not-a-real-key-7c41"
BY_ID = {task.id: task for task in SANDBOX_TASKS}
CODE_TASKS = [task for task in SANDBOX_TASKS if "check.py" in task.files]
PAGE = _PAGE


# =================================================================================================
# FR-179: the task set
# =================================================================================================


def test_the_sandbox_kind_is_24_tasks_in_five_groups_beside_the_12_m15_tasks():
    assert len(SANDBOX_TASKS) == 24, [t.id for t in SANDBOX_TASKS]
    ids = [t.id for t in SANDBOX_TASKS]
    assert len(set(ids)) == 24 and not set(ids) & {t.id for t in TASKS}
    assert len(TASKS) == 12, "the M15 tasks stay"
    assert {t.group for t in SANDBOX_TASKS} == set(GROUPS)
    for group in GROUPS:
        assert sum(1 for t in SANDBOX_TASKS if t.group == group) >= 2, group
    for task in SANDBOX_TASKS:
        assert task.kind == SANDBOX and task.prompt.strip() and task.max_turns >= 1 and callable(task.checker)


def test_only_the_fetch_tasks_reach_out_and_only_to_example_com():
    for task in SANDBOX_TASKS:
        assert task.egress == ((FETCH_HOST,) if task.group == "fetch" else ()), task.id
    assert FETCH_HOST == "example.com:443"


def _verify_here(task: SandboxTask, files: dict, tmp_path: pathlib.Path) -> str:
    """The harness's verifier, run on the host over an outcome the test wrote -- test-authored code,
    never a model's -- with a fresh nonce: what the verifier container does in a live comparison."""
    import shutil
    import uuid as _uuid

    root = tmp_path / _uuid.uuid4().hex[:8]
    for path, data in {**{f"work/{p}": d for p, d in files.items()}, "verify/verify.py": VERIFIER.encode(),
                       **{f"verify/{p}": d for p, d in (task.verification_files or {}).items()}}.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    nonce = _uuid.uuid4().hex
    done = subprocess.run([sys.executable, "-I", str(root / "verify" / "verify.py"), str(root / "work")],
                          input=verification_input(task, nonce), capture_output=True, timeout=120)
    shutil.rmtree(root, ignore_errors=True)
    return verification_result(done.stdout, nonce)


def judged(task: SandboxTask, which: str, tmp_path: pathlib.Path) -> Workspace:
    """An outcome as the harness hands it to the checker: verified when it is a code task, with the
    witness and the evidence of a fetch when it is a fetch task (DECISION-b944d3ab, DECISION-7830ca10)."""
    space = task.outcome(which)
    verified = _verify_here(task, dict(space.files), tmp_path) if task.verification else None
    fetch = task.group == "fetch"
    return task.outcome(which, verified=verified, witness=PAGE if fetch else None,
                        fetched={FETCH_NAME} if fetch else frozenset())


@pytest.mark.parametrize("task", [t for t in SANDBOX_TASKS if t.group == "fetch"], ids=lambda t: t.id)
def test_a_fetch_passes_only_with_evidence_of_the_fetch(task, tmp_path):
    """M50 round 1, note 3: the page exactly as served, with no evidence the run reached the host, is a
    page reproduced from memory and fails; so does evidence of another host only."""
    good = judged(task, "good", tmp_path)
    assert task.checker(good) is True
    assert task.checker(task.outcome("good", witness=PAGE)) is False, "a page with no fetch behind it passed"
    assert task.checker(task.outcome("good", witness=PAGE, fetched={"example.org"})) is False
    assert task.checker(task.outcome("good", witness=PAGE, fetched={FETCH_NAME, "example.org"})) is True


@pytest.mark.parametrize("task", SANDBOX_TASKS, ids=lambda t: t.id)
def test_every_checker_accepts_its_good_outcome_and_refuses_its_bad_and_tricky(task, tmp_path):
    """FR-180: the three outcomes, and a run that left nothing, and one that only answered."""
    assert task.checker(judged(task, "good", tmp_path)) is True
    assert task.checker(judged(task, "bad", tmp_path)) is False
    assert task.checker(judged(task, "tricky", tmp_path)) is False
    assert task.checker(Workspace(files={})) is False
    assert task.checker(Workspace(files=dict(task.files), answer="done, all checks pass")) is False, \
        "an answer alone over the untouched files passed"
    assert task.checker(None) is False and task.checker(task.files) is False


@pytest.mark.parametrize("task", SANDBOX_TASKS, ids=lambda t: t.id)
def test_each_outcome_differs_from_the_others(task):
    """A tricky outcome identical to the good or the bad one would test nothing of its own."""
    good, bad, tricky = (dict(task.outcome(w).files) for w in ("good", "bad", "tricky"))
    assert good != bad and good != tricky and bad != tricky


def _write(root: pathlib.Path, files: dict) -> None:
    for path, data in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _files(root: pathlib.Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts}


def _run_check(task: SandboxTask, changes: dict, root: pathlib.Path, check: bytes | None = None) -> dict:
    """The task's own check.py, run on the host over files the test wrote: the task's own code,
    never a model's. Returns the folder's files afterwards."""
    files = applied(task.files, changes)
    if check is not None:
        files["check.py"] = check
    _write(root, files)
    subprocess.run([sys.executable, "check.py"], cwd=root, capture_output=True, timeout=60)
    return _files(root)


def _module(task: SandboxTask) -> str:
    return next(p for p in task.files if p.endswith(".py") and p != "check.py")


@pytest.mark.parametrize("task", CODE_TASKS, ids=lambda t: t.id)
def test_the_tasks_own_check_and_the_verifier_both_pass_the_good_code(task, tmp_path):
    found = _run_check(task, {_module(task): task.good[_module(task)]}, tmp_path / "run")
    assert "check.txt" in found, "the check did not pass the good code"
    assert _verify_here(task, found, tmp_path) == "pass"
    assert task.checker(Workspace(files=found, verified="pass")) is True


@pytest.mark.parametrize("task", CODE_TASKS, ids=lambda t: t.id)
def test_the_tasks_own_check_and_the_verifier_both_fail_the_given_and_the_wrong_code(task, tmp_path):
    for n, code in enumerate((task.files[_module(task)], task.bad[_module(task)])):
        found = _run_check(task, {_module(task): code}, tmp_path / f"run{n}")
        assert "check.txt" not in found
        verified = _verify_here(task, found, tmp_path)
        assert verified != "pass" and task.checker(Workspace(files=found, verified=verified)) is False


@pytest.mark.parametrize("task", CODE_TASKS, ids=lambda t: t.id)
def test_a_check_changed_to_pass_anything_is_refused(task, tmp_path):
    """The run may not change its own check, even when its code is right."""
    lenient = task.files["check.py"].replace(b"if got != expected:", b"if False:")
    assert lenient != task.files["check.py"]
    found = _run_check(task, {_module(task): task.good[_module(task)]}, tmp_path / "run", check=lenient)
    assert _verify_here(task, found, tmp_path) == "pass", "the control: the code itself is right"
    assert task.checker(Workspace(files=found, verified="pass")) is False


_WRONG = b"def seconds(text):\n    return -1\n"
_TABLE = b"def seconds(text):\n    return {'1h30m': 5400, '45s': 45, '2h': 7200, '1h2m3s': 3723, '10m': 600}[text]\n"
_WRITER = (b"import hashlib\nopen('check.txt', 'w').write('PASS ' + hashlib.sha256(open(__file__, 'rb').read())"
           b".hexdigest() + chr(10))\n\n\ndef seconds(text):\n    return -1\n")
# Pre-review F2's routes past check.txt, on parse-duration: each passes the run's own check, or
# forges what it writes, and each must fail the verifier.
_FORGERIES = {
    "forged check.txt": {"duration.py": _WRONG, "check.txt": b"PASS " + hashlib.sha256(_WRONG).hexdigest().encode()
                         + b"\n"},
    "import-time writer": {"duration.py": _WRITER},
    "shadowing package": {"duration/__init__.py": _TABLE},
    "lookup table": {"duration.py": _TABLE},
}


@pytest.mark.parametrize("route", sorted(_FORGERIES))
def test_no_route_past_check_txt_passes_the_verifier(route, tmp_path):
    task = BY_ID["parse-duration"]
    found = _run_check(task, _FORGERIES[route], tmp_path / "run")
    assert "check.txt" in found, f"the control: {route} gets past the run's own check"
    if route != "forged check.txt":
        assert found["duration.py"] in (_WRITER, _TABLE, task.files["duration.py"])
    verified = _verify_here(task, found, tmp_path)
    assert verified != "pass", (route, verified)
    assert task.checker(Workspace(files=found, verified=verified)) is False


def test_a_verification_without_its_nonce_or_with_a_failed_case_is_not_a_pass():
    task = BY_ID["roman-numerals"]
    assert verification_result(b"VERIFIED abc 8 8\n", "abc") == "pass"
    assert verification_result(b"VERIFIED abc 8 8\n", "xyz") == "no verification", "a line another nonce names"
    assert verification_result(b"VERIFIED abc 7 8\n", "abc") == "7 of 8 cases"
    assert verification_result(b"VERIFIED abc 0 0\n", "abc") != "pass"
    assert verification_result(b"", "abc") == "no verification"
    cases = task.verification[2]
    assert len(cases) == 8, "five visible cases and three hidden"
    visible = task.files["check.py"].decode()
    assert not any(repr(arguments) in visible for arguments, _ in cases[5:]), "a hidden case is in check.py"


@pytest.mark.parametrize("task", [t for t in SANDBOX_TASKS if t.group == "fetch"], ids=lambda t: t.id)
def test_a_fetch_is_judged_against_the_witness_and_refused_without_one(task):
    assert task.checker(task.outcome("good", witness=PAGE, fetched={FETCH_NAME})) is True
    assert task.checker(task.outcome("good", fetched={FETCH_NAME})) is False, "judged with no witness"
    other = PAGE.replace(b"documentation examples", b"illustrative examples")
    assert task.checker(task.outcome("good", witness=other, fetched={FETCH_NAME})) is False, "a page other than the one served"


class _Served:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size):
        return self.body[:size]


def test_the_witness_is_the_page_fetched_once_with_its_time():
    asked = []

    def opener(request, timeout):
        asked.append((request.full_url, timeout))
        return _Served(PAGE)

    body, when = fetch_witness(opener=opener)
    assert body == PAGE and asked == [("https://example.com/", 30)] and when.endswith("+00:00")
    with pytest.raises(ValueError):
        fetch_witness(opener=lambda request, timeout: _Served(b"x" * (MAX_TRANSFER_BYTES + 1)))


@pytest.mark.parametrize("task", SANDBOX_TASKS, ids=lambda t: t.id)
def test_every_tasks_files_are_a_workspace_the_preset_accepts(task):
    space = WorkspaceSpec(image=FAKE_IMAGE, files=dict(task.files), egress=task.egress)
    assert dict(space.files) == dict(task.files)


def test_the_budget_rule_is_ten_times_the_m15_medians_recomputed_from_that_report():
    data = json.loads((HERE / "reports" / M15_REPORT).read_text(encoding="utf-8"))
    by_kind: dict = {}
    for row in data["rows"]:
        if row["arm"] == "agentsdk":
            by_kind.setdefault(row["kind"], []).append(row)
    assert set(by_kind) == set(M15_MEDIANS)
    for kind, rows in by_kind.items():
        usd = statistics.median(Decimal(r["cost_usd"]) for r in rows)
        tokens = statistics.median(r["prompt_tokens"] + r["completion_tokens"] for r in rows)
        assert (Decimal(M15_MEDIANS[kind][0]), M15_MEDIANS[kind][1]) == (usd, tokens), kind
        assert ceilings(kind) == (str(usd * 10), int(tokens * 10))
    assert ceilings(SANDBOX) == ceilings("multi-step") == ("0.023940", 20175)


# =================================================================================================
# This SDK's arm: the general preset, offline on FakeProvider
# =================================================================================================


def _registry(input_price="0.0000001"):
    pricing = ModelPricing(input=Decimal(input_price), output=Decimal(input_price))
    return ModelRegistry([ModelEntry(provider="test", model_id=MODEL, model_version="1", adapter_version="test/1",
                                     capabilities=ModelCapabilities(max_context_tokens=200_000, pricing=pricing))])


def _respond(content="", calls=()):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(50, 10, 60))


class Scripted:
    """A model that makes the given turns of tool calls, then answers."""

    def __init__(self, turns, answer="done"):
        self.turns, self.answer = turns, answer

    async def send(self, request):
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn < len(self.turns):
            return _respond(calls=[ToolCall(id=f"t{turn}-{i}", name=name, arguments=args)
                                   for i, (name, args) in enumerate(self.turns[turn])])
        return _respond(self.answer)


async def _rm(cmd):
    for path in cmd.argv[1:]:
        cmd.files.remove(path)
    return 0


class CountingProvider(FakeProvider):
    """FakeProvider, counting each export and the paths it was asked for."""

    def __init__(self):
        super().__init__(commands={"rm": _rm})
        self.exports: list[str] = []

    async def export(self, handle, path):
        self.exports.append(path)
        return await super().export(handle, path)


def _good_turns(task: SandboxTask):
    """Writes the good outcome's files, removes what it removes, then tries an export."""
    writes = [("write_workspace_file", {"path": p, "content": d.decode()}) for p, d in task.good.items() if d is not None]
    removed = [p for p, d in task.good.items() if d is None]
    turns = [[w] for w in writes]
    if removed:
        turns.append([("run_command", {"argv": ["rm", *removed]})])
    turns.append([("export_workspace_file", {"path": next(iter(task.good))
                   if task.good[next(iter(task.good))] is not None else writes[0][1]["path"]})])
    return turns


def _arm(model, provider=None, registry=None):
    provider = provider or CountingProvider()
    return PresetArm(client_factory=lambda: model, model=MODEL, registry=registry or _registry(), image=FAKE_IMAGE,
                     provider=provider), provider


def test_this_sdks_arm_builds_the_general_preset_fr_179_names():
    arm, _ = _arm(Scripted([]))
    for task in (BY_ID["fetch-page"], BY_ID["repair-average"]):
        agent = arm.agent_for(task)
        spec, space = agent.spec, agent.spec.workspace
        assert spec.preset == ("general", GENERAL_VERSION) and spec.tool_profile == GENERAL_TOOLS
        assert space.egress == task.egress and dict(space.files) == dict(task.files) and space.keep is True
        assert space.image == FAKE_IMAGE
        assert agent.max_turns == task.max_turns
        assert (agent.budget.run_ceiling_usd, agent.budget.run_ceiling_tokens) == (Decimal("0.023940"), 20175)
        config = agent.config(TENANT, PROJECT)
        assert (config.tenant_id, config.project_id) == ("adr12", "comparison")


@pytest.mark.parametrize("task_id", ["count-error-lines", "rename-notes", "repair-average"])
async def test_this_sdks_arm_runs_a_task_refuses_its_export_and_reads_the_kept_workspace_back(task_id, monkeypatch,
                                                                                            tmp_path):
    from agentsdk import Runner

    # The arm releases the kept workspace itself, after reading it back (FR-151's `released`).
    # aclose() would remove it anyway, so the provider being empty afterwards cannot show it.
    released, original = [], Runner.release_workspace

    async def release(self, tenant, project, workspace_id):
        done = await original(self, tenant, project, workspace_id)
        released.append((tenant, project, workspace_id, done, list(provider.exports)))
        return done

    monkeypatch.setattr(Runner, "release_workspace", release)
    task = BY_ID[task_id]
    arm, provider = _arm(Scripted(_good_turns(task)))
    outcome = await arm.run_sandbox(task)
    verified = _verify_here(task, dict(outcome.files), tmp_path) if task.verification else None
    assert released == [("adr12", "comparison", outcome.workspace_id, True, [""])], \
        "not released once, by its recorded id, after the read-back"
    assert outcome.status == "completed", outcome.error
    assert task.checker(Workspace(files=dict(outcome.files), answer=outcome.answer, verified=verified)) is True
    assert outcome.files == applied(task.files, task.good)
    assert outcome.exports_refused == 1, "the one export was refused, and recorded"
    assert provider.exports == [""], "read back through one export of the whole workspace"
    assert await provider.list_owned({"agentsdk.workspace": outcome.workspace_id}) == [], "not released"
    assert (outcome.budget_usd, outcome.budget_tokens) == ("0.023940", 20175)


async def test_the_refusal_is_recorded_with_its_principal(monkeypatch):
    """Each refusal is the Runner's own record: a decision, not an approval, by the approver."""
    from agentsdk import Runner

    seen = []
    original = Runner.interruptions

    async def recording(self, tenant, project, **kwargs):
        views = await original(self, tenant, project, **kwargs)
        if kwargs.get("pending_only") is False:
            seen.extend(views)
        return views

    monkeypatch.setattr(Runner, "interruptions", recording)
    task = BY_ID["count-error-lines"]
    arm, _ = _arm(Scripted(_good_turns(task)))
    await arm.run_sandbox(task)
    assert seen, "the patch never fired"
    exports = [v for v in seen if v.interruption.tool_name == "export_workspace_file"]
    assert len(exports) == 1 and len(seen) == 1, "only the export waited; its commands did not"
    assert exports[0].decision.decision != "approve" and exports[0].decision.principal == APPROVER


async def test_a_run_its_budget_stopped_is_reported_as_budget_exceeded():
    task = BY_ID["count-error-lines"]
    arm, provider = _arm(Scripted(_good_turns(task)), registry=_registry(input_price="0.001"))
    outcome = await arm.run_sandbox(task)
    assert outcome.status == "budget_exceeded" and outcome.turns == 1, (outcome.status, outcome.error)
    assert await provider.list_owned({"agentsdk.workspace": outcome.workspace_id}) == []


def _tar(members) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
async def test_the_budget_rule_applies_to_every_m15_kind_on_this_sdks_arm(task, tmp_path):
    """FR-179: one rule for every task of a kind; the M15 tasks keep their tools and fixture."""
    from evals.adr12.arms import ThisSdkArm
    from evals.adr12.tasks import FIXTURE_ROOT
    import shutil

    shutil.copytree(FIXTURE_ROOT, tmp_path / "root")
    arm = ThisSdkArm(client_factory=lambda: Scripted([], task.good_answer), model=MODEL, model_registry=_registry(),
                     budgets=True)
    outcome = await arm.run(task, tmp_path / "root")
    assert outcome.status == "completed" and (outcome.budget_usd, outcome.budget_tokens) == ceilings(task.kind)
    stopped = await ThisSdkArm(client_factory=lambda: Scripted([[("glob_files", {"pattern": "*"})]], "x"),
                               model=MODEL, model_registry=_registry(input_price="0.001"), budgets=True
                               ).run(task, tmp_path / "root")
    assert stopped.status == "budget_exceeded", (stopped.status, stopped.error)
    unbudgeted = await ThisSdkArm(client_factory=lambda: Scripted([], task.good_answer), model=MODEL,
                                  model_registry=_registry()).run(task, tmp_path / "root")
    assert (unbudgeted.budget_usd, unbudgeted.budget_tokens) == (None, None), "M15's own runs had no budget"


def test_the_read_back_parses_the_checked_tar_in_memory_and_refuses_a_link(monkeypatch):
    def nowhere(*args, **kwargs):
        raise AssertionError("a workspace was extracted to the host")

    monkeypatch.setattr(tarfile.TarFile, "extract", nowhere)
    monkeypatch.setattr(tarfile.TarFile, "extractall", nowhere)
    folder = tarfile.TarInfo("./sub")
    folder.type = tarfile.DIRTYPE
    one = tarfile.TarInfo("./sub/a.txt")
    one.size = 3
    assert read_back(_tar([(folder, None), (one, b"abc")])) == {"sub/a.txt": b"abc"}
    link = tarfile.TarInfo("./out")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    with pytest.raises(ValueError):
        read_back(_tar([(link, None)]))


# =================================================================================================
# The Claude arm: its container, against a recording stand-in for the docker client
# =================================================================================================


class RecordingDocker:
    """Records every docker call; plays the container's part from what was copied in."""

    def __init__(self, *, fail_start=False, driver_lines=None, written=None):
        self.calls: list[dict] = []
        self.fail_start = fail_start
        self.driver_lines = driver_lines
        self.written = written or {}
        self.work: dict[str, bytes] = {}

    async def __call__(self, argv, *, stdin=None, env=None, timeout=120, limit=None):
        DockerCli.checked(list(argv))
        self.calls.append({"argv": list(argv), "stdin": stdin, "env": dict(env or {})})
        if argv[0] == "cp" and argv[1] == "-":
            self.work = read_back(stdin)
        elif argv[0] == "start":
            if self.fail_start:
                raise RuntimeError("the container did not start")
            self.work.update(self.written)
            lines = self.driver_lines if self.driver_lines is not None else [
                {"kind": "assistant", "text": "7"},
                {"kind": "result", "num_turns": 3, "usage": {"input_tokens": 100, "output_tokens": 20,
                                                             "cache_read_input_tokens": 30},
                 "total_cost_usd": 0.0012, "subtype": "success", "is_error": False}]
            return 0, "".join(json.dumps(line) + "\n" for line in lines).encode(), b""
        elif argv[0] == "cp":
            return 0, files_into_work(self.work), b""
        return 0, b"", b""


def _claude(docker, **options):
    return ClaudeContainerArm(image=CLAUDE_IMAGE, model="bedrock.anthropic.claude-haiku-4-5",
                              base_url="https://gateway.example/", auth_token=KEY, docker=docker, **options)


async def test_the_claude_arm_copies_the_files_in_runs_the_driver_reads_back_and_removes_the_container():
    task = BY_ID["count-error-lines"]
    docker = RecordingDocker(written={"answer.txt": b"7\n"})
    outcome = await _claude(docker).run_sandbox(task)
    verbs = [c["argv"][0] for c in docker.calls]
    assert verbs == ["create", "cp", "start", "cp", "rm"], verbs
    name = docker.calls[0]["argv"][docker.calls[0]["argv"].index("--name") + 1]
    assert name.startswith("adr12-claude-")
    assert docker.calls[1]["argv"] == ["cp", "-", f"{name}:/work"]
    assert docker.calls[2]["argv"] == ["start", "-a", "-i", name]
    assert docker.calls[3]["argv"] == ["cp", f"{name}:/work/.", "-"]
    assert docker.calls[4]["argv"] == ["rm", "-f", name]
    assert outcome.files == applied(task.files, {"answer.txt": b"7\n"})
    assert task.checker(Workspace(files=dict(outcome.files), answer=outcome.answer)) is True
    assert (outcome.status, outcome.turns, outcome.answer) == ("completed", 3, "7")
    assert outcome.usage.prompt_tokens == 130 and outcome.sdk_cost_estimate == Decimal("0.0012")


async def test_the_claude_arms_container_is_made_without_pulling_and_given_the_key_by_name_only():
    task = BY_ID["fetch-page"]
    docker = RecordingDocker()
    await _claude(docker).run_sandbox(task)
    create = docker.calls[0]["argv"]
    assert create[:3] == ["create", "--pull", "never"]
    assert f"{CLAUDE_LABEL}=1" in create and create[-1] == CLAUDE_IMAGE
    assert create[create.index(CLAUDE_KEY_VARIABLE) - 1] == "-e", "the key is named, its value read from env"
    for call in docker.calls:
        assert not any(KEY in part for part in call["argv"]), call["argv"]
        assert call["stdin"] is None or KEY.encode() not in call["stdin"]
    assert docker.calls[0]["env"] == {CLAUDE_KEY_VARIABLE: KEY}
    assert all(not c["env"] for c in docker.calls[1:]), "the key went to a call that does not need it"


async def test_the_claude_arm_is_given_its_tools_turns_and_model_on_its_standard_input():
    task = BY_ID["repair-average"]
    docker = RecordingDocker()
    await _claude(docker).run_sandbox(task)
    request = json.loads(docker.calls[2]["stdin"])
    assert request == {"prompt": task.prompt, "max_turns": task.max_turns, "tools": list(CLAUDE_SANDBOX_TOOLS),
                       "model": "bedrock.anthropic.claude-haiku-4-5", "cwd": "/work"}
    assert list(CLAUDE_SANDBOX_TOOLS) == ["Read", "Glob", "Grep", "Bash", "Write", "Edit"]
    members = {m.name: m for m in tarfile.open(fileobj=io.BytesIO(docker.calls[1]["stdin"]))}
    assert {n for n, m in members.items() if m.isfile()} == set(task.files)
    assert all(m.uid == CLAUDE_UID and m.gid == CLAUDE_UID for m in members.values())


async def test_the_container_is_removed_when_the_run_fails():
    docker = RecordingDocker(fail_start=True)
    outcome = await _claude(docker).run_sandbox(BY_ID["unique-names"])
    assert [c["argv"][0] for c in docker.calls][-1] == "rm"
    assert outcome.status == "failed" and "did not start" in outcome.error


async def test_a_driver_that_reports_max_turns_or_an_error_is_reported_so():
    docker = RecordingDocker(driver_lines=[{"kind": "result", "num_turns": 16, "subtype": "error_max_turns",
                                            "is_error": True}])
    assert (await _claude(docker).run_sandbox(BY_ID["roman-numerals"])).status == "max_turns_exceeded"
    docker = RecordingDocker(driver_lines=[{"kind": "error", "error": "CLIConnectionError"}])
    outcome = await _claude(docker).run_sandbox(BY_ID["roman-numerals"])
    assert outcome.status == "failed" and "CLIConnectionError" in outcome.error


@pytest.mark.parametrize("argv", [["build", "-t", "x", "."], ["pull", "python"], ["push", "x"], ["run", "x"],
                                  ["image", "pull", "x"], ["image", "build", "."], ["buildx", "build", "."],
                                  ["create", "--name", "x", "img"], ["exec", "x", "sh"], [],
                                  ["create", "--pull", "always", "img"], ["create", "--pull=always", "img"],
                                  ["create", "--pull", "never", "--pull", "always", "img"],
                                  ["rm", "-f", "someones-container"], ["rm", "-f", "adr12-claude-1", "postgres"],
                                  ["rm", "adr12-claude-1"], ["ps", "-a"],
                                  ["ps", "-a", "--format", "{{.Names}}", "--filter", "label=other=1"]])
def test_the_docker_client_refuses_everything_but_what_the_arm_needs(argv):
    """AC-108: nothing here pulls or builds an image, and create never pulls one it lacks."""
    with pytest.raises(ValueError):
        DockerCli.checked(argv)


def test_no_python_under_the_harness_names_a_docker_pull_or_build():
    """Every list literal in the harness's code, read by its syntax tree: none starts a pull or a build."""
    # Pre-review F7: anywhere in a list, not only first: ["docker", "image", "pull", ...] is one.
    forbidden = {"build", "pull", "push", "buildx", "load", "import", "--pull=always", "--pull=missing"}
    seen = 0
    for path in HERE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.List, ast.Tuple)):
                seen += 1
                for element in node.elts:
                    if isinstance(element, ast.Constant) and isinstance(element.value, str):
                        assert element.value not in forbidden, (path.name, node.lineno, element.value)
    assert seen > 20, "the walk saw the harness's lists"


def test_the_static_scan_catches_a_pull_anywhere_in_a_list():
    """The test of the test: the scan's rule flags what F7 named."""
    for source in ('x = ["docker", "image", "pull", "img"]', 'x = ("docker", "build", ".")', 'x = ["push"]'):
        found = [e.value for n in ast.walk(ast.parse(source)) if isinstance(n, (ast.List, ast.Tuple))
                 for e in n.elts if isinstance(e, ast.Constant)]
        assert {"pull", "build", "push"} & set(found), source


# --- the driver, run with a scripted query ------------------------------------------------------


class _Said:
    def __init__(self, kind, **fields):
        self.kind = kind
        vars(self).update(fields)


def test_the_driver_gives_the_sdk_exactly_its_tools_turns_folder_and_the_environment_it_names():
    pytest.importorskip("claude_agent_sdk")
    request = {"prompt": "p", "max_turns": 12, "tools": list(CLAUDE_SANDBOX_TOOLS), "model": "m", "cwd": "/work"}
    environ = {"ANTHROPIC_BASE_URL": "https://g/", CLAUDE_KEY_VARIABLE: KEY, "ANTHROPIC_MODEL": "m",
               "HOME": "/home/agent", "PATH": "/usr/bin"}
    options = claude_driver.options_for(request, environ)
    assert options.tools == options.allowed_tools == list(CLAUDE_SANDBOX_TOOLS)
    assert (options.permission_mode, options.setting_sources, options.cwd, options.model, options.max_turns) == \
        ("dontAsk", [], "/work", "m", 12)
    assert options.cli_path == claude_driver.CLI_PATH
    assert options.env == {"ANTHROPIC_BASE_URL": "https://g/", CLAUDE_KEY_VARIABLE: KEY, "ANTHROPIC_MODEL": "m"}


async def test_the_driver_emits_each_message_and_reports_an_error_by_its_type_alone():
    emitted = []

    async def query(*, prompt, options):
        yield _Said("assistant", text="working")
        yield _Said("result", num_turns=2, usage={"input_tokens": 5}, total_cost_usd=0.1, subtype="success",
                    is_error=False)

    assert await claude_driver.drive({"prompt": "p"}, query, None, emitted.append) == 0
    assert [e["kind"] for e in emitted] == ["assistant", "result"] and emitted[1]["num_turns"] == 2

    async def failing(*, prompt, options):
        raise RuntimeError(f"connection refused with {KEY}")
        yield  # pragma: no cover

    emitted.clear()
    assert await claude_driver.drive({"prompt": "p"}, failing, None, emitted.append) == 1
    assert emitted == [{"kind": "error", "error": "RuntimeError"}], "the message, and the key in it, stay inside"


# --- the image's Dockerfile -----------------------------------------------------------------------


def _instructions() -> list[tuple[str, str]]:
    found = []
    for line in (HERE / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", " ").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            word, _, rest = line.partition(" ")
            found.append((word.upper(), rest.strip()))
    return found


def test_the_claude_image_is_built_on_the_workspace_image_with_the_pinned_cli_and_sdk_and_no_credential():
    steps = _instructions()
    assert steps[0] == ("FROM", WORKSPACE_IMAGE), "the same base as this SDK's workspace image"
    assert not [s for s in steps if s[0] in ("ENV", "ARG", "ADD")], "no variable or remote file in the image"
    body = " ".join(rest for _, rest in steps)
    for word in ("KEY", "TOKEN", "SECRET", "PASSWORD", "ANTHROPIC_", "MODEL_API"):
        assert word not in body.upper(), word
    pin = json.loads((HERE / "package.json").read_text(encoding="utf-8"))["dependencies"]["@anthropic-ai/claude-code"]
    assert pin == "2.1.267" and CLAUDE_IMAGE == f"adr12-claude:cli-{pin}"
    assert ("COPY", "package.json package-lock.json ./") in steps and "npm ci" in body
    sdk = [line for line in (HERE / "requirements.txt").read_text(encoding="utf-8").splitlines()
           if line.startswith("claude-agent-sdk==")]
    assert sdk and f"pip install --no-cache-dir {sdk[0]}" in body
    assert ("USER", "agent") in steps and "adduser -D -u 1000 agent" in body and CLAUDE_UID == 1000
    # M50 live run 1: Claude Code's Bash tool runs in bash, which Alpine does not ship.
    assert "apk add --no-cache nodejs npm bash" in body
    assert steps[-1] == ("ENTRYPOINT", '["python", "/opt/adr12/driver.py"]')
    assert "COPY claude_driver.py ./driver.py" in [f"{w} {r}" for w, r in steps]
    ignored = (HERE / ".dockerignore").read_text(encoding="utf-8").split()
    assert "node_modules" in ignored and "reports" in ignored, "the build context carries no host install or report"


# =================================================================================================
# The harness and the report (FR-179: task count, setups, the budget rule, networking, images)
# =================================================================================================


class StandInClaude:
    """The Claude arm's place in the harness, returning a fixed outcome."""

    name = "claude-agent-sdk"
    wire_format = "Anthropic Messages through the Claude Code CLI"
    version = "claude-agent-sdk 0.2.152 with Claude Code CLI 2.1.267, in stand-in"

    async def run_sandbox(self, task):
        return SandboxOutcome(answer="x", status="completed", turns=2, usage=Usage(10, 2, 12), wall_ms=1.0,
                              files=applied(task.files, task.bad))


class LocalVerifier:
    """The verifier's program run on the host over the scripted outcomes the test wrote."""

    def __init__(self, tmp_path):
        self.tmp_path, self.calls = tmp_path, []

    async def verify(self, task, files):
        self.calls.append(task.id)
        return _verify_here(task, dict(files), self.tmp_path)


def test_the_harness_runs_the_sandbox_tasks_and_the_report_states_what_fr_179_names(tmp_path):
    tasks = [BY_ID["count-error-lines"], BY_ID["repair-average"]]

    class Runaway(PresetArm):
        def __init__(self):
            super().__init__(client_factory=lambda: self._model_for(), model=MODEL, registry=_registry(),
                             image=FAKE_IMAGE, provider=CountingProvider())
            self._next = iter([Scripted(_good_turns(tasks[0])), Scripted(_good_turns(tasks[1]))])

        def _model_for(self):
            return next(self._next)

    from agentsdk.registry import ModelPricing as Pricing

    comparison = harness.run_comparison(
        arms=[], tasks=[], repetitions=1, pricing=Pricing(input="0.000001", output="0.000005"),
        reports_dir=tmp_path / "reports", workdir=tmp_path / "work", gateway_model="g",
        sandbox_arms=[Runaway(), StandInClaude()], sandbox_tasks=tasks, stamp="2026-11-08T00:00:00+00:00",
        verifier=LocalVerifier(tmp_path),
        image_digests={"agentsdk": WORKSPACE_IMAGE, "claude-agent-sdk": CLAUDE_IMAGE + " sha256:abc"})
    rows = comparison.rows
    assert [(r["task"], r["arm"], r["passed"]) for r in rows] == [
        ("count-error-lines", "agentsdk", True), ("count-error-lines", "claude-agent-sdk", False),
        ("repair-average", "agentsdk", True), ("repair-average", "claude-agent-sdk", False)]
    ours = [r for r in rows if r["arm"] == "agentsdk"]
    assert all(r["exports_refused"] == 1 and r["budget_usd"] == "0.023940" and r["budget_tokens"] == 20175
               and r["group"] in GROUPS and r["files_read_back"] >= 1 for r in ours)
    theirs = [r for r in rows if r["arm"] == "claude-agent-sdk"]
    assert all(r["budget_usd"] == "unavailable" and r["exports_refused"] == "unavailable" for r in theirs)
    meta = comparison.metadata
    assert meta["task_count"] == 2 and meta["kinds"] == {"sandbox": 2}
    assert meta["preset_version"] == GENERAL_VERSION and meta["ceilings"]["sandbox"] == {"usd": "0.023940",
                                                                                         "tokens": 20175}
    text = comparison.markdown_path.read_text(encoding="utf-8")
    for needle in ("2 tasks: 2 sandbox.", "Budget rule:", "Networking:", "ordinary networking", "general-v4",
                   WORKSPACE_IMAGE, "sha256:abc", "## By kind", "0.023940", "20175"):
        assert needle in text, needle


def test_the_report_names_every_kind_present_and_its_setup(tmp_path):
    metadata = {"date": "2026-11-08", "arms": ["agentsdk", "claude-agent-sdk"], "task_count": 36,
                "kinds": {"decline": 1, "long": 3, "multi-step": 4, "sandbox": 24, "single-step": 4},
                "setups": {k: (harness.SANDBOX_SETUP if k == "sandbox" else harness.M15_SETUP)
                           for k in ("decline", "long", "multi-step", "sandbox", "single-step")},
                "ceilings": {k: dict(zip(("usd", "tokens"), ceilings(k)))
                             for k in ("decline", "long", "multi-step", "sandbox", "single-step")},
                "budget_rule": harness.BUDGET_RULE, "networking": harness.NETWORKING, "preset_version": "general-v1",
                "images": {}}
    rows = [{"task": "t", "kind": "sandbox", "arm": "agentsdk", "passed": False, "status": "budget_exceeded",
             "turns": 3, "wall_ms": 1.0, "cost_usd": "0.03", "sdk_cost_estimate_usd": "unavailable", "answer": "x"}]
    text = report.build_markdown(metadata, rows)
    assert "36 tasks: 1 decline, 3 long, 4 multi-step, 24 sandbox, 4 single-step." in text
    assert "budget_exceeded 1" in text, "a run its budget stopped is its own terminal status"
    for kind in metadata["kinds"]:
        assert f"| {kind} |" in text


def test_sandbox_tasks_without_their_arms_are_refused(tmp_path):
    from agentsdk.registry import ModelPricing as Pricing

    with pytest.raises(ValueError):
        harness.run_comparison(arms=[], tasks=[], repetitions=1, pricing=Pricing(input="0", output="0"),
                               reports_dir=tmp_path, workdir=tmp_path, gateway_model="g",
                               sandbox_tasks=[SANDBOX_TASKS[0]])


def test_code_tasks_without_a_verifier_and_fetch_tasks_without_a_witness_are_refused(tmp_path):
    from agentsdk.registry import ModelPricing as Pricing

    common = dict(arms=[], tasks=[], repetitions=1, pricing=Pricing(input="0", output="0"), reports_dir=tmp_path,
                  workdir=tmp_path, gateway_model="g", sandbox_arms=[StandInClaude()])
    with pytest.raises(ValueError, match="verifier"):
        harness.run_comparison(**common, sandbox_tasks=[BY_ID["roman-numerals"]])
    with pytest.raises(ValueError, match="witness"):
        harness.run_comparison(**common, sandbox_tasks=[BY_ID["fetch-page"]])


# =================================================================================================
# The pre-review's repairs (DECISION-b944d3ab)
# =================================================================================================


@pytest.mark.parametrize("failing", ["decide", "interruptions"])
async def test_an_approver_call_that_raises_is_tried_again_and_the_run_ends_at_once(failing, monkeypatch):
    """F1: the approver outlives a failed read or refusal; the refusal is still recorded."""
    from agentsdk import Runner

    original, failed = getattr(Runner, failing), []

    async def once(self, *args, **kwargs):
        if not failed and kwargs.get("pending_only", True) is not False:
            failed.append(failing)
            raise RuntimeError("simulated")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(Runner, failing, once)
    task = BY_ID["count-error-lines"]
    arm, _ = _arm(Scripted(_good_turns(task)))
    outcome = await asyncio.wait_for(arm.run_sandbox(task), 20)
    assert failed == [failing], "the patch never fired"
    assert outcome.status == "completed" and outcome.exports_refused == 1, (outcome.status, outcome.error)


async def test_a_run_whose_approver_task_dies_still_ends_with_its_own_result(monkeypatch):
    """F1: whatever the approver ends with never replaces the run's own end."""
    from evals.adr12 import sandbox_arms

    async def dies(runner, run_id):
        raise RuntimeError("the approver died")

    monkeypatch.setattr(sandbox_arms, "_refuse_every_export", dies)
    task = BY_ID["unique-names"]
    arm, _ = _arm(Scripted([[("write_workspace_file", {"path": "unique.txt",
                                                       "content": task.good["unique.txt"].decode()})]]))
    outcome = await asyncio.wait_for(arm.run_sandbox(task), 20)
    assert outcome.status == "completed" and task.checker(Workspace(files=dict(outcome.files))) is True


def _script(tmp_path: pathlib.Path, name: str, body: str) -> None:
    (tmp_path / name).write_text(body, encoding="utf-8")


async def test_the_docker_clients_output_is_bounded(tmp_path, monkeypatch):
    """F3: a copy out past its bound ends the call; it is never read whole into memory. The
    client runs a stand-in program named for its subcommand, in a folder of its own."""
    monkeypatch.chdir(tmp_path)
    _script(tmp_path, "cp", "import sys\nchunk = b'x' * 65536\nfor _ in range(400):\n"
                            "    sys.stdout.buffer.write(chunk)\n    sys.stdout.buffer.flush()\n")
    import time

    # The real interpreter, not the venv's launcher, which would leave its child holding the pipes.
    client = DockerCli(executable=os.path.join(sys.base_prefix, "python.exe"))
    started = time.monotonic()
    with pytest.raises(ValueError, match="over"):
        await client(["cp", "adr12-claude-x:/work/.", "-"], limit=1_000_000)
    # Ended at once: its pipes drained after the kill, so its wait does not sit out a deadline.
    assert time.monotonic() - started < 10, time.monotonic() - started
    code, out, _ = await client(["cp", "adr12-claude-x:/work/.", "-"], limit=40 * 1024 * 1024)
    assert code == 0 and len(out) == 400 * 65536, "the control: under its bound the whole output comes back"


class SlowCreate(RecordingDocker):
    """A daemon whose create is slow: cancelling the arm then must not leave the container."""

    async def __call__(self, argv, *, stdin=None, env=None, timeout=120, limit=None):
        if argv[0] == "create":
            self.calls.append({"argv": list(argv), "stdin": stdin, "env": dict(env or {})})
            await asyncio.sleep(30)
        return await super().__call__(argv, stdin=stdin, env=env, timeout=timeout, limit=limit)


async def test_a_cancel_during_create_still_removes_the_container_and_tries_again():
    """F4: the removal is shielded from the cancel and repeated, for a create the daemon finishes late."""
    docker = SlowCreate()
    running = asyncio.ensure_future(_claude(docker).run_sandbox(BY_ID["unique-names"]))
    await asyncio.sleep(0.2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await asyncio.sleep(3.5)
    name = docker.calls[0]["argv"][docker.calls[0]["argv"].index("--name") + 1]
    removals = [c["argv"] for c in docker.calls if c["argv"][0] == "rm"]
    assert removals == [["rm", "-f", name]] * 3, removals


async def test_the_sweep_removes_whatever_the_comparisons_labels_still_name():
    from evals.adr12.sandbox_arms import sweep

    class Listing(RecordingDocker):
        async def __call__(self, argv, *, stdin=None, env=None, timeout=120, limit=None):
            DockerCli.checked(list(argv))
            self.calls.append({"argv": list(argv)})
            if argv[0] == "ps":
                return 0, (b"adr12-claude-aaa\n" if "adr12.claude-arm" in argv[-1] else b"adr12-verify-bbb\n"), b""
            return 0, b"", b""

    docker = Listing()
    assert await sweep(docker) == ["adr12-claude-aaa", "adr12-verify-bbb"]
    assert [c["argv"] for c in docker.calls if c["argv"][0] == "rm"] == [["rm", "-f", "adr12-claude-aaa"],
                                                                        ["rm", "-f", "adr12-verify-bbb"]]


async def test_the_key_never_reaches_a_recorded_error_or_answer(tmp_path):
    """F5: a program in the container can print the key it was given; the arm records none of it."""
    class Leaking(RecordingDocker):
        async def __call__(self, argv, *, stdin=None, env=None, timeout=120, limit=None):
            done = await super().__call__(argv, stdin=stdin, env=env, timeout=timeout, limit=limit)
            if argv[0] == "start":
                return 3, b"", f"boom token={KEY}".encode()
            return done

    leaked = await _claude(Leaking(driver_lines=[])).run_sandbox(BY_ID["unique-names"])
    assert KEY not in (leaked.error or "") and "[redacted]" in leaked.error, leaked.error
    said = await _claude(RecordingDocker(driver_lines=[
        {"kind": "assistant", "text": f"{CLAUDE_KEY_VARIABLE}={KEY}"},
        {"kind": "result", "num_turns": 1, "subtype": "success", "is_error": False}])).run_sandbox(BY_ID["unique-names"])
    assert said.answer == f"{CLAUDE_KEY_VARIABLE}=[redacted]"
    from agentsdk.registry import ModelPricing as Pricing

    class Arm(StandInClaude):
        async def run_sandbox(self, task):
            return said

    # In a thread of its own: the harness runs each arm on a loop of its own, as the live command does.
    comparison = await asyncio.to_thread(
        harness.run_comparison, arms=[], tasks=[], repetitions=1, pricing=Pricing(input="0", output="0"),
        reports_dir=tmp_path / "r", workdir=tmp_path / "w", gateway_model="g", sandbox_arms=[Arm()],
        sandbox_tasks=[BY_ID["unique-names"]])
    assert comparison.rows[0]["answer"] == f"{CLAUDE_KEY_VARIABLE}=[redacted]", "the answer reached the report"
    for path in (comparison.json_path, comparison.markdown_path):
        text = path.read_text(encoding="utf-8")
        assert KEY not in text and "[redacted]" in text


async def test_a_driver_that_exits_without_a_result_says_so():
    outcome = await _claude(RecordingDocker(driver_lines=[])).run_sandbox(BY_ID["unique-names"])
    assert outcome.status == "failed" and outcome.error == "the driver reported no result"


def test_a_run_that_raises_is_a_failed_row_and_the_report_is_still_written(tmp_path):
    """F6: one run's failure loses nothing already measured."""
    from agentsdk.registry import ModelPricing as Pricing

    class Raising(StandInClaude):
        name = "agentsdk"

        async def run_sandbox(self, task):
            raise RuntimeError("the arm broke")

    comparison = harness.run_comparison(arms=[], tasks=[], repetitions=1, pricing=Pricing(input="0", output="0"),
                                        reports_dir=tmp_path / "r", workdir=tmp_path / "w", gateway_model="g",
                                        sandbox_arms=[Raising(), StandInClaude()],
                                        sandbox_tasks=[BY_ID["unique-names"]])
    assert [(r["arm"], r["status"], r["error"]) for r in comparison.rows][0] == (
        "agentsdk", "failed", "the arm raised RuntimeError")
    assert len(comparison.rows) == 2 and comparison.json_path.exists()


async def test_a_release_that_raises_is_the_rows_error(monkeypatch):
    from agentsdk import Runner

    async def broken(self, tenant, project, workspace_id):
        raise RuntimeError("store down")

    monkeypatch.setattr(Runner, "release_workspace", broken)
    task = BY_ID["count-error-lines"]
    arm, provider = _arm(Scripted(_good_turns(task)))
    outcome = await arm.run_sandbox(task)
    assert outcome.error == "release: RuntimeError: store down" and outcome.files == applied(task.files, task.good)
    assert await provider.list_owned({"agentsdk.workspace": outcome.workspace_id}) == [], "aclose() removed it"


def test_a_comparison_that_stops_writes_what_it_measured(tmp_path, monkeypatch):
    """F6: a stop that is not one run's failure -- an interrupt -- still leaves its partial report."""
    from agentsdk.registry import ModelPricing as Pricing

    calls = []

    def interrupting(arm, task, repetition, pricing, verifier=None, witness=None):
        if calls:
            raise KeyboardInterrupt
        calls.append(task.id)
        return {"task": task.id, "kind": task.kind, "arm": arm.name, "passed": False, "status": "failed",
                "turns": 0, "wall_ms": 0.0, "cost_usd": "0", "sdk_cost_estimate_usd": "unavailable", "answer": "x"}

    monkeypatch.setattr(harness, "_one_sandbox_run", interrupting)
    with pytest.raises(KeyboardInterrupt):
        harness.run_comparison(arms=[], tasks=[], repetitions=1, pricing=Pricing(input="0", output="0"),
                               reports_dir=tmp_path / "r", workdir=tmp_path / "w", gateway_model="g",
                               sandbox_arms=[StandInClaude()], sandbox_tasks=[BY_ID["unique-names"],
                                                                              BY_ID["largest-file"]],
                               stamp="2026-11-08T00:00:00+00:00")
    written = json.loads((tmp_path / "r" / "adr12-comparison-2026-11-08.json").read_text(encoding="utf-8"))
    assert len(written["rows"]) == 1 and "1 runs measured" in written["metadata"]["incomplete"]


async def test_the_verifier_runs_in_a_fresh_container_with_no_network_and_is_removed():
    """F2: the verifier's container, its argv, its payload and its removal."""
    class Verifying(RecordingDocker):
        async def __call__(self, argv, *, stdin=None, env=None, timeout=120, limit=None):
            DockerCli.checked(list(argv))
            self.calls.append({"argv": list(argv), "stdin": stdin})
            if argv[0] == "start":
                nonce = stdin.decode().splitlines()[0]
                return 0, f"noise\nVERIFIED {nonce} 8 8\n".encode(), b""
            return 0, b"", b""

    docker = Verifying()
    task = BY_ID["roman-numerals"]
    result = await Verifier(image=WORKSPACE_IMAGE, docker=docker).verify(task, applied(task.files, task.good))
    assert result == "pass"
    create, copied, started, removed = (c["argv"] for c in docker.calls)
    for flag in (["--pull", "never"], ["--network", "none"], ["--cap-drop", "ALL"], ["--entrypoint", "python"]):
        assert any(create[i:i + 2] == flag for i in range(len(create))), flag
    assert WORKSPACE_IMAGE in create and create[-2:] == ["-I", "/verify/verify.py"]
    name = create[create.index("--name") + 1]
    assert name.startswith("adr12-verify-") and copied == ["cp", "-", f"{name}:/"] and removed == ["rm", "-f", name]
    members = {m.name for m in tarfile.open(fileobj=io.BytesIO(docker.calls[1]["stdin"])) if m.isfile()}
    assert members == {f"work/{p}" for p in applied(task.files, task.good)} | {"verify/verify.py"}
    nonce = docker.calls[2]["stdin"].decode().splitlines()[0]
    assert len(nonce) == 32 and not any(nonce.encode() in d for d in applied(task.files, task.good).values())


async def test_a_verifier_that_cannot_run_is_not_a_pass_and_still_removes_its_container():
    class Failing(RecordingDocker):
        async def __call__(self, argv, *, stdin=None, env=None, timeout=120, limit=None):
            self.calls.append({"argv": list(argv)})
            if argv[0] == "start":
                raise TimeoutError
            return 0, b"", b""

    docker = Failing()
    task = BY_ID["roman-numerals"]
    result = await Verifier(image=WORKSPACE_IMAGE, docker=docker).verify(task, dict(task.files))
    assert result == "no verification: TimeoutError" and docker.calls[-1]["argv"][:2] == ["rm", "-f"]


# =================================================================================================
# NFR-51: eval-only
# =================================================================================================


def test_nothing_in_the_package_imports_names_or_builds_the_claude_arm():
    for path in (REPO / "agentsdk").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in ("evals.adr12", "evals/adr12", "claude_agent_sdk", "adr12-claude", "Dockerfile"):
            assert needle not in text, (path, needle)


# =================================================================================================
# Docker: this SDK's arm on DockerProvider, and the Claude arm's container on the built image
# =================================================================================================


def _docker_ready() -> str | None:
    try:
        done = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"docker is not reachable: {type(exc).__name__}"
    return None if done.returncode == 0 else "docker is not running"


@pytest.mark.docker
async def test_this_sdks_arm_on_docker_lands_the_files_runs_reads_back_and_releases():
    assert _docker_ready() is None, _docker_ready()
    from agentsdk.workspace.docker import DockerProvider

    task = BY_ID["count-error-lines"]
    turns = [[("run_command", {"argv": ["sh", "-c", "grep -c ERROR logs/app.log > answer.txt"]})],
             [("export_workspace_file", {"path": "answer.txt"})]]
    provider = DockerProvider()
    arm = PresetArm(client_factory=lambda: Scripted(turns, "7"), model=MODEL, registry=_registry(),
                    image=WORKSPACE_IMAGE, provider=provider)
    outcome = await arm.run_sandbox(task)
    assert outcome.status == "completed", outcome.error
    assert outcome.files == {**task.files, "answer.txt": b"7\n"}, sorted(outcome.files)
    assert task.checker(Workspace(files=dict(outcome.files))) is True
    assert outcome.exports_refused == 1
    assert await provider.list_owned({"agentsdk.workspace": outcome.workspace_id}) == [], "the workspace remains"


# The stand-in driver: a program the image's own Python runs in the driver's place, reading the
# request, working in /work and printing the driver's lines. It reaches no gateway.
STAND_IN = (
    "import json,os,sys;"
    "r=json.loads(sys.stdin.readline());"
    "n=sum(1 for l in open('logs/app.log') if 'ERROR' in l);"
    "open('answer.txt','w').write(str(n)+chr(10));"
    "print(json.dumps({'kind':'assistant','text':str(n)}));"
    "print(json.dumps({'kind':'result','num_turns':1,'subtype':'success','is_error':False,"
    "'cwd':os.getcwd(),'uid':os.getuid(),'tools':r['tools'],"
    "'key_set':bool(os.environ.get('" + CLAUDE_KEY_VARIABLE + "')),"
    "'bash':__import__('shutil').which('bash')}))"
)


@pytest.mark.docker
async def test_the_claude_arms_container_on_the_built_image_takes_the_files_and_is_read_back_and_removed():
    assert _docker_ready() is None, _docker_ready()
    docker = DockerCli()
    code, out, _ = await docker(["image", "inspect", "--format", "{{json .Config}}", CLAUDE_IMAGE])
    assert code == 0, (f"the Claude arm's image is not built: the owner builds it with "
                       f"docker build -t {CLAUDE_IMAGE} evals/adr12 (no gate pulls or builds it)")
    config = json.loads(out)
    # No credential in the image: it adds no variable to its base's environment (whose GPG_KEY is
    # CPython's public release-signing key id), and none of its own layers names a key.
    _, base, _ = await docker(["image", "inspect", "--format", "{{json .Config}}", WORKSPACE_IMAGE])
    assert config.get("Env") == json.loads(base).get("Env"), "the image adds to its base's environment"
    own = subprocess.run(["docker", "image", "history", "--no-trunc", "--format", "{{.CreatedBy}}", CLAUDE_IMAGE],
                         capture_output=True, text=True, timeout=60).stdout.splitlines()
    based = subprocess.run(["docker", "image", "history", "--no-trunc", "--format", "{{.CreatedBy}}", WORKSPACE_IMAGE],
                           capture_output=True, text=True, timeout=60).stdout.splitlines()
    added = own[:len(own) - len(based)]
    assert added and own[len(own) - len(based):] == based, "the image is not built on the workspace image"
    assert not [step for step in added if any(w in step.upper() for w in ("KEY", "TOKEN", "SECRET", "PASSWORD"))], \
        "a layer of the image names a credential"
    task = BY_ID["count-error-lines"]
    lines = []

    class Watching:
        async def __call__(self, argv, **kwargs):
            done = await docker(argv, **kwargs)
            if argv[0] == "start":
                lines.extend(json.loads(l) for l in done[1].decode().splitlines() if l.startswith("{"))
            return done

    arm = ClaudeContainerArm(image=CLAUDE_IMAGE, model="m", base_url="https://gateway.invalid", auth_token=KEY,
                             docker=Watching(), command=("python", "-c", STAND_IN))
    outcome = await arm.run_sandbox(task)
    assert outcome.error is None, outcome.error
    assert outcome.files == {**task.files, "answer.txt": b"7\n"}
    result = lines[-1]
    assert (result["cwd"], result["uid"], result["key_set"]) == ("/work", CLAUDE_UID, True)
    assert result["tools"] == list(CLAUDE_SANDBOX_TOOLS)
    assert result["bash"], "the Bash tool has no bash in the image"
    left = subprocess.run(["docker", "ps", "-a", "--filter", f"label={CLAUDE_LABEL}", "--format", "{{.Names}}"],
                          capture_output=True, text=True, timeout=60)
    assert left.stdout.strip() == "", f"containers left: {left.stdout}"


@pytest.mark.docker
async def test_the_verifier_container_passes_good_code_and_refuses_every_forgery():
    """F2 on Docker: the workspace image runs the verifier with no network; nothing is left."""
    assert _docker_ready() is None, _docker_ready()
    task = BY_ID["parse-duration"]
    verifier = Verifier(image=WORKSPACE_IMAGE)
    assert await verifier.verify(task, applied(task.files, task.good)) == "pass"
    for route, changes in sorted(_FORGERIES.items()):
        result = await verifier.verify(task, applied(task.files, changes))
        assert result != "pass", (route, result)
    totals = BY_ID["category-totals"]
    assert await verifier.verify(totals, applied(totals.files, totals.good)) == "pass", "the hidden data file"
    left = subprocess.run(["docker", "ps", "-a", "--filter", "label=adr12.verifier=1", "--format", "{{.Names}}"],
                          capture_output=True, text=True, timeout=60)
    assert left.stdout.strip() == "", f"verifier containers left: {left.stdout}"


def test_setup_states_each_arms_user():
    """M50 round 1, note 2: FR-179's report states the arms' asymmetries, the users among them. The text is
    built from the constants that set each user, and the Claude image's Dockerfile creates that same uid."""
    from agentsdk.workspace.docker import UID
    from evals.adr12.sandbox_arms import CLAUDE_UID

    assert (UID, CLAUDE_UID) == (65534, 1000)
    ours, theirs = harness.SANDBOX_SETUP["agentsdk"], harness.SANDBOX_SETUP["claude-agent-sdk"]
    assert f"uid {UID} (nobody" in ours and "no bash" in ours, ours
    assert f"uid {CLAUDE_UID} (agent" in theirs and "bash (for the Bash tool)" in theirs, theirs
    dockerfile = (HERE / "Dockerfile").read_text(encoding="utf-8")
    assert f"adduser -D -u {CLAUDE_UID} agent" in dockerfile and "USER agent" in dockerfile
    stored = json.loads((HERE / "reports" / "adr12-comparison-2026-11-08.json").read_text(encoding="utf-8"))
    assert stored["metadata"]["setups"]["sandbox"] == harness.SANDBOX_SETUP, "the stored report was not corrected"
    assert stored["metadata"]["corrections"], "a corrected report says so"


# =================================================================================================
# M51: proof of fetch (M50 round 1, note 3; DECISION-7830ca10)
# =================================================================================================


class FetchingProvider(CountingProvider):
    """The fake with example.com served through its proxy, and a `curl` that connects through it."""

    def __init__(self):
        FakeProvider.__init__(self, commands={"rm": _rm, "curl": _curl}, sites={("example.com", 443): PAGE},
                              resolve={"example.com": ["93.184.215.14"]})
        self.exports = []


async def _curl(cmd):
    """curl -o FILE https://example.com/ -- the page through the workspace's proxy, as served."""
    target, url = cmd.argv[2], cmd.argv[3]
    host = url.split("//", 1)[1].split("/", 1)[0]
    try:
        body = await cmd.connect(host, 443)
    except OSError:
        cmd.err("refused\n")
        return 7
    cmd.files.write(target, body)
    return 0


def _fetch_turns(*, fetch: bool):
    """fetch-page: the page through curl, or typed out as served with no fetch behind it."""
    first = (("run_command", {"argv": ["curl", "-o", "page.html", "https://example.com/"]}) if fetch
             else ("write_workspace_file", {"path": "page.html", "content": PAGE.decode()}))
    return [[first], [("write_workspace_file", {"path": "status.txt", "content": "200\n"})],
            [("export_workspace_file", {"path": "page.html"})]]


@pytest.mark.parametrize("fetch", [True, False], ids=["fetched", "from-memory"])
async def test_this_sdks_arm_shows_a_fetch_by_its_recorded_connection(fetch):
    """The evidence is the workspace's allowed connection, read from the store after the release; a
    page typed out exactly as served, with no connection, is no fetch."""
    task = BY_ID["fetch-page"]
    arm, provider = _arm(Scripted(_fetch_turns(fetch=fetch)), provider=FetchingProvider())
    outcome = await arm.run_sandbox(task)
    assert outcome.status == "completed", outcome.error
    assert outcome.files["page.html"] == PAGE, "the page is the one served either way"
    assert outcome.fetched == (frozenset({FETCH_NAME}) if fetch else frozenset())
    space = Workspace(files=dict(outcome.files), witness=PAGE, fetched=outcome.fetched)
    assert task.checker(space) is fetch
    # Round-1 F1: the row carries the connection that counted, to be checked against the store.
    if fetch:
        assert len(outcome.fetch_evidence) == 1
        assert outcome.fetch_evidence[0].startswith("connection ")
        assert outcome.fetch_evidence[0].endswith(f" to {FETCH_NAME}:443, allowed")
    else:
        assert outcome.fetch_evidence == ()


async def test_this_sdks_arm_counts_only_an_allowed_connection_of_its_own_run(monkeypatch):
    """A refused connection, or another run's, is not this run's fetch."""
    from agentsdk.workspace.records import ConnectionRecord
    from evals.adr12 import sandbox_arms

    fields = {name: None for name in ConnectionRecord.__dataclass_fields__}
    rows = [ConnectionRecord(**{**fields, "host": "example.com", "allowed": False, "run_id": "r1"}),
            ConnectionRecord(**{**fields, "host": "example.org", "allowed": True, "run_id": "r2"}),
            ConnectionRecord(**{**fields, "host": "example.net", "allowed": True, "run_id": "r1"})]

    class Store:
        def connections(self, tenant, project, run_id=None):
            assert (tenant, project, run_id) == (TENANT, PROJECT, "r1")
            return rows

    class Holder:
        def _workspace_store(self):
            return Store()

    assert await sandbox_arms.reached_hosts(Holder(), "r1") == frozenset({"example.net"})


def _call(name, key, value, error=False, reported=True):
    tools = [{"id": "c1", "name": name, "input": {key: value}}]
    results = [{"kind": "results", "results": [{"id": "c1", "is_error": error}]}] if reported else []
    return tools, results


CURL = "curl -s -o page.html https://example.com/"


@pytest.mark.parametrize("call, expected", [
    (_call("Bash", "command", CURL), {FETCH_NAME}),
    (_call("Bash", "command", "python3 -c \"import urllib.request as u; u.urlopen('https://example.com/')\""),
     {FETCH_NAME}),
    (_call("WebFetch", "url", "https://example.com/"), {FETCH_NAME}),
    (_call("Bash", "command", CURL, error=True), set()),
    (_call("Bash", "command", CURL, reported=False), set()),
    (_call("Bash", "command", "echo example.com"), set()),
    (_call("Bash", "command", "echo https://example.com/"), set()),
    (_call("Bash", "command", "curl -s https://example.com.evil.org/"), set()),
    (_call("Bash", "command", "curl -s https://notexample.com/"), set()),
    (_call("Write", "content", "https://example.com/"), set()),
    (_call("NotebookEdit", "url", "https://example.com/"), set()),
    (_call("Bash", "command", "curl -s https://example.org/"), set()),
    (([], []), set()),
    # Round-1 F1: the program run, not the text. The four the verdict reproduced as counted...
    (_call("Bash", "command", "echo 'curl -s https://example.com/ -o page.html' > notes.sh"), set()),
    (_call("Bash", "command", "# curl https://example.com/ then ls -la"), set()),
    (_call("Bash", "command", "python3 -c \"print('https://example.com/')\""), set()),
    (_call("Bash", "command", "grep -n https://example.com/ notes.txt && echo curl"), set()),
    # ...and the three it reproduced as missed.
    (_call("Bash", "command", "curl -sL example.com -o page.html"), {FETCH_NAME}),
    (_call("Bash", "command", "curl -s https://www.example.com/ -o page.html"), {FETCH_NAME}),
    (_call("Bash", "command", "u=https://exam; curl -s ${u}ple.com/ -o page.html"), {FETCH_NAME}),
    # The forms a run writes, each a fetch.
    (_call("Bash", "command", "U=https://example.com/; curl -s \"$U\" -o page.html"), {FETCH_NAME}),
    (_call("Bash", "command", "cd /work && curl -s https://example.com/ -o page.html && wc -c page.html"),
     {FETCH_NAME}),
    (_call("Bash", "command", "/usr/bin/wget -q -O page.html https://example.com/"), {FETCH_NAME}),
    (_call("Bash", "command", "timeout 30 curl -s https://example.com/ > page.html"), {FETCH_NAME}),
    (_call("Bash", "command", "python3 - <<'EOF'\nimport urllib.request\n"
                              "urllib.request.urlretrieve('https://example.com/', 'page.html')\nEOF"), {FETCH_NAME}),
    (_call("Bash", "command", "sh -c 'curl -s https://example.com/ -o page.html'"), {FETCH_NAME}),
    (_call("Bash", "command", "echo \"$(curl -s https://example.com/)\" > page.html"), {FETCH_NAME}),
    (_call("Bash", "command", "node -e \"fetch('https://example.com/').then(r => r.text())"
                              ".then(t => console.log(t))\" > page.html"), {FETCH_NAME}),
    # And the forms that run nothing on the host.
    (_call("Bash", "command", "echo '$(curl -s https://example.com/)'"), set()),
    (_call("Bash", "command", "python3 -c \"import requests\"; echo https://example.com/"), set()),
    (_call("Bash", "command", "curl -s -o example.com.html https://example.org/"), set()),
    (_call("Bash", "command", "curl -s -o example.com -e https://example.com/ https://example.org/"), set()),
    (_call("Bash", "command", "ls -la # ; curl -s https://example.com/ -o page.html"), set()),
    (_call("Bash", "command", "curl 'https://example.com/"), set()),
    (_call("Bash", "command", "cat <<'EOF'\ncurl -s https://example.com/\nEOF"), set()),
], ids=["curl", "python", "webfetch", "an-error", "no-result", "echo-name", "echo-url", "longer-host",
        "prefixed-host", "write-names-it", "not-a-fetcher", "another-host", "no-call",
        "echo-a-curl-line", "a-comment", "print-the-url", "grep-then-echo-curl",
        "no-scheme", "www", "joined-url",
        "quoted-variable", "and-chain", "wget-by-path", "timeout-wrapped", "python-heredoc", "sh-c",
        "command-substitution", "node-fetch",
        "single-quoted-substitution", "library-and-url-apart", "host-as-a-filename",
        "host-as-option-values", "a-comment-holding-a-command", "unbalanced-quote",
        "heredoc-to-cat"])
async def test_the_claude_arm_shows_a_fetch_only_by_a_fetching_call_that_succeeded(call, expected):
    """Pre-review F1 and round-1 F1: a fetch is a downloader, or a runtime whose code uses an HTTP
    library, actually run on the host; a call that only names the host, a failed or unreported call, or
    a look-alike host is no fetch, and the page reproduced from memory then fails. The call that counted
    is the row's evidence."""
    tools, results = call
    task = BY_ID["fetch-page"]
    lines = [{"kind": "assistant", "text": "", "tools": tools}, *results,
             {"kind": "result", "num_turns": 2, "usage": {"input_tokens": 1}, "total_cost_usd": 0.0,
              "subtype": "success", "is_error": False}]
    docker = RecordingDocker(driver_lines=lines, written={"page.html": PAGE, "status.txt": b"200\n"})
    outcome = await _claude(docker).run_sandbox(task)
    assert outcome.fetched == frozenset(expected)
    assert task.checker(Workspace(files=dict(outcome.files), witness=PAGE, fetched=outcome.fetched)) is bool(expected)
    said = next(iter(tools[0]["input"].values())) if tools else None
    assert outcome.fetch_evidence == ((f"{tools[0]['name']}: {said}",) if expected else ())


def _script_run(*steps):
    """Calls in order, each reported a success: ("Write", path, content), ("Edit", path), ("Bash", command)."""
    tools = []
    for number, step in enumerate(steps):
        given = ({"file_path": step[1], "content": step[2]} if step[0] == "Write" else
                 {"file_path": step[1], "old_string": "a", "new_string": "b"} if step[0] == "Edit" else
                 {"command": step[1]})
        tools.append({"id": f"c{number}", "name": step[0], "input": given})
    results = [{"id": tool["id"], "is_error": False} for tool in tools]
    return [{"kind": "assistant", "text": "", "tools": tools}, {"kind": "results", "results": results}]


FETCH_PY = "import urllib.request\nurllib.request.urlretrieve('https://example.com/', 'page.html')\n"


@pytest.mark.parametrize("steps, expected", [
    ((("Write", "/work/fetch.py", FETCH_PY), ("Bash", "cd /work && python3 fetch.py")), {FETCH_NAME}),
    ((("Write", "/work/get.sh", "curl -s https://example.com/ -o page.html\n"), ("Bash", "bash get.sh")),
     {FETCH_NAME}),
    ((("Write", "/work/fetch.py", FETCH_PY), ("Edit", "/work/fetch.py"), ("Bash", "python3 fetch.py")), set()),
    ((("Bash", "python3 fetch.py"),), set()),
    ((("Write", "/work/fetch.py", "print('https://example.com/')\n"), ("Bash", "python3 fetch.py")), set()),
    ((("Bash", "cat > fetch.py <<'EOF'\n" + FETCH_PY + "EOF"), ("Bash", "python3 fetch.py")), {FETCH_NAME}),
    ((("Write", "fetch.py", FETCH_PY), ("Bash", "echo 'print(1)' > fetch.py"), ("Bash", "python3 fetch.py")), set()),
    ((("Write", "/work/fetch.py", FETCH_PY.replace("example.com", "example.org")), ("Bash", "python3 fetch.py")),
     set()),
], ids=["written-python", "written-shell", "edited-after-written", "never-written", "written-but-no-fetch",
        "written-by-cat", "overwritten-by-a-redirect", "fetches-another-host"])
def test_a_script_the_run_wrote_is_read_from_its_write_call(steps, expected):
    """A run that writes its fetch to a file and runs it fetched; a file edited since is not known."""
    from evals.adr12.sandbox_arms import named_hosts

    assert named_hosts(_script_run(*steps), ("example.com:443",)) == frozenset(expected)


def test_the_report_lists_each_fetch_rows_evidence():
    """Round-1 F1: a fetch credit can be checked from the report itself."""
    base = {"kind": "sandbox", "status": "completed", "turns": 2, "wall_ms": 1.0, "cost_usd": "0.01",
            "sdk_cost_estimate_usd": "unavailable", "answer": "done", "repetition": 1}
    rows = [{**base, "task": "fetch-page", "group": "fetch", "arm": "claude-agent-sdk", "passed": True,
             "fetched": [FETCH_NAME], "fetch_evidence": ["Bash: curl -s https://example.com/ | tee page.html"]},
            {**base, "task": "fetch-page", "group": "fetch", "arm": "agentsdk", "passed": False,
             "fetched": [], "fetch_evidence": []},
            {**base, "task": "count-error-lines", "group": "shell", "arm": "agentsdk", "passed": True,
             "fetched": [], "fetch_evidence": []}]
    text = report.build_markdown({"date": "2026-11-09", "arms": ["agentsdk", "claude-agent-sdk"]}, rows)
    section = text.split("## Fetch evidence", 1)[1].split("\n## ", 1)[0]
    assert "| fetch-page | claude-agent-sdk | 1 | yes | example.com | Bash: curl -s https://example.com/ \\| tee page.html |" in section
    assert "| fetch-page | agentsdk | 1 | no | none | none |" in section
    assert "count-error-lines" not in section


def test_the_claude_arms_evidence_never_carries_the_key():
    task = BY_ID["fetch-page"]
    command = f"curl -s -H 'x-api-key: {KEY}' https://example.com/ -o page.html"
    tools, results = _call("Bash", "command", command)
    lines = [{"kind": "assistant", "text": "", "tools": tools}, *results,
             {"kind": "result", "num_turns": 2, "usage": {"input_tokens": 1}, "total_cost_usd": 0.0,
              "subtype": "success", "is_error": False}]
    docker = RecordingDocker(driver_lines=lines, written={"page.html": PAGE, "status.txt": b"200\n"})
    outcome = asyncio.run(_claude(docker).run_sandbox(task))
    assert outcome.fetched == frozenset({FETCH_NAME})
    assert KEY not in json.dumps(outcome.fetch_evidence) and "[redacted]" in outcome.fetch_evidence[0]


def test_the_driver_reports_each_tool_call_and_whether_its_result_was_an_error_never_the_result():
    from claude_agent_sdk import AssistantMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

    call = ToolUseBlock(id="t1", name="Bash", input={"command": "curl https://example.com/"})
    message = AssistantMessage(content=[TextBlock(text="fetching"), call], model="m")
    said = [{"id": "t1", "name": "Bash", "input": {"command": "curl https://example.com/"}}]
    assert claude_driver.describe(message) == {"kind": "assistant", "text": "fetching", "tools": said}
    assert claude_driver.describe(AssistantMessage(content=[call], model="m"))["tools"] == said
    assert claude_driver.describe(AssistantMessage(content=[TextBlock(text=" ")], model="m")) is None
    results = UserMessage(content=[ToolResultBlock(tool_use_id="t1", content=f"the key {KEY}", is_error=False),
                                   ToolResultBlock(tool_use_id="t2", content="boom", is_error=True),
                                   ToolResultBlock(tool_use_id="t3", content="?")])
    assert claude_driver.describe(results) == {"kind": "results", "results": [
        {"id": "t1", "is_error": False}, {"id": "t2", "is_error": True}, {"id": "t3", "is_error": False}]}
    assert KEY not in json.dumps(claude_driver.describe(results)), "a result's content left the container"
    assert claude_driver.describe(UserMessage(content="text only")) is None


@pytest.mark.parametrize("fetched", [frozenset({FETCH_NAME}), frozenset()], ids=["shown", "not-shown"])
def test_the_harness_gives_the_checker_the_arms_evidence_and_records_it(fetched):
    task = BY_ID["fetch-page"]

    class Arm:
        name = "agentsdk"

        async def run_sandbox(self, _task):
            return SandboxOutcome(answer=None, status="completed", turns=2, usage=Usage(), wall_ms=1.0,
                                  files={"page.html": PAGE, "status.txt": b"200\n"}, fetched=fetched,
                                  fetch_evidence=("Bash: " + CURL,) if fetched else ())

    row = harness._one_sandbox_run(Arm(), task, 1, None, None, (PAGE, "2026-11-08T00:00:00+00:00"))
    assert row["passed"] is bool(fetched) and row["fetched"] == sorted(fetched)
    assert row["fetch_evidence"] == (["Bash: " + CURL] if fetched else [])


def test_the_report_gives_each_kind_and_the_whole_its_rates_statuses_turns_tokens_cost_and_time():
    """FR-181: per kind and overall, for each arm, each measure as measured."""
    metadata = {"date": "2026-11-08", "arms": ["agentsdk", "claude-agent-sdk"], "task_count": 2,
                "kinds": {"multi-step": 1, "sandbox": 1},
                "setups": {"multi-step": harness.M15_SETUP, "sandbox": harness.SANDBOX_SETUP}, "images": {}}

    def row(kind, arm, passed, status, turns, tokens, cost, wall):
        return {"task": kind + "-task", "kind": kind, "arm": arm, "passed": passed, "status": status, "turns": turns,
                "prompt_tokens": tokens, "completion_tokens": 10 if tokens is not None else None, "wall_ms": wall,
                "cost_usd": cost, "sdk_cost_estimate_usd": "unavailable", "answer": ""}

    rows = [row("multi-step", "agentsdk", True, "completed", 3, 100, "0.01", 1000.0),
            row("multi-step", "agentsdk", False, "budget_exceeded", 7, 300, "0.03", 3000.0),
            row("sandbox", "agentsdk", True, "completed", 5, 200, "0.02", 2000.0),
            row("sandbox", "claude-agent-sdk", False, "failed", 4, None, "unavailable", 500.0)]
    text = report.build_markdown(metadata, rows)
    table = text.split("## By kind", 1)[1].split("## By task", 1)[0]
    assert ("| multi-step | agentsdk | 1 | 50% | 2 | budget_exceeded 1, completed 1 | 5.0 | 420 | 0.04 | 2000.0 |"
            in table), table
    assert "| sandbox | claude-agent-sdk | 0 | 0% | 1 | failed 1 | 4 | unavailable | unavailable | 500.0 |" in table
    assert ("| all | agentsdk | 2 | 67% | 3 | budget_exceeded 1, completed 2 | 5 | 630 | 0.06 | 2000.0 |" in table), table
    assert "| all | claude-agent-sdk | 0 | 0% | 1 | failed 1 |" in table
    assert "| multi-step | claude-agent-sdk | 0 | unavailable | 0 | none |" in table, "a kind an arm never ran"


def test_a_report_never_replaces_another_of_the_same_date(tmp_path):
    """M51: the final comparison overwrote M50's report of the same day; a second report is -2, then -3."""
    metadata, rows = {"date": "2026-11-08T10:00:00+00:00", "arms": []}, []
    first = report.write_report(metadata, rows, tmp_path)
    (tmp_path / "keep.marker").write_text("x")
    first_text = first[0].read_text(encoding="utf-8")
    second = report.write_report({**metadata, "date": "2026-11-08T11:00:00+00:00"}, rows, tmp_path)
    third = report.write_report(metadata, rows, tmp_path)
    assert [p.name for p in (*first, *second, *third)] == [
        "adr12-comparison-2026-11-08.json", "adr12-comparison-2026-11-08.md",
        "adr12-comparison-2026-11-08-2.json", "adr12-comparison-2026-11-08-2.md",
        "adr12-comparison-2026-11-08-3.json", "adr12-comparison-2026-11-08-3.md"]
    assert first[0].read_text(encoding="utf-8") == first_text, "the first report was rewritten"


def test_a_report_takes_no_name_either_of_whose_files_exists(tmp_path):
    """Pre-review F5: each file is claimed exclusively; a name half taken is skipped and nothing of it
    is overwritten or left behind."""
    (tmp_path / "adr12-comparison-2026-11-08.md").write_text("someone else's")
    (tmp_path / "adr12-comparison-2026-11-08-2.json").write_text("another's")
    paths = report.write_report({"date": "2026-11-08", "arms": []}, [], tmp_path)
    assert [p.name for p in paths] == ["adr12-comparison-2026-11-08-3.json", "adr12-comparison-2026-11-08-3.md"]
    assert (tmp_path / "adr12-comparison-2026-11-08.md").read_text() == "someone else's"
    assert (tmp_path / "adr12-comparison-2026-11-08-2.json").read_text() == "another's"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "adr12-comparison-2026-11-08-2.json", "adr12-comparison-2026-11-08-3.json", "adr12-comparison-2026-11-08-3.md",
        "adr12-comparison-2026-11-08.md"], "a claimed file of a skipped name was left"
