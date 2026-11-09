"""M51 (FR-182, DECISION-7830ca10): tuning the general preset, offline.

The log keeps each version with the models it ran on; a changed text under a logged version is
refused; the stop rule holds a version only when it keeps, on both models, every task its
predecessor passed, and stops tuning at the third version after general-v1."""

from __future__ import annotations

import json

import pytest

from agentsdk.model import Usage
from agentsdk.presets import GENERAL_INSTRUCTIONS, GENERAL_VERSION
from evals.adr12 import tune
from evals.adr12.sandbox import FETCH_NAME, SANDBOX_TASKS, _PAGE, applied
from evals.adr12.sandbox_arms import SandboxOutcome

BY_ID = {task.id: task for task in SANDBOX_TASKS}
HAIKU, MINI = tune.HAIKU, tune.MINI


def _item(version, model, passed, failed=(), text=None):
    rows = [{"task": t, "passed": True, "status": "completed", "cost_usd": "0.01"} for t in passed]
    rows += [{"task": t, "passed": False, "status": "failed", "cost_usd": "unavailable"} for t in failed]
    return tune.entry(version, text if text is not None else f"text of {version}", model, rows, "r.json", "d")


def test_the_two_models_are_two_upstream_providers_and_each_is_priced():
    assert set(tune.MODELS) == {HAIKU, MINI}
    assert {model.split(".", 1)[0] for model in tune.MODELS} == {"bedrock", "openai"}
    from evals.adr12.run_comparison import GATEWAY_MODEL

    assert HAIKU == GATEWAY_MODEL, "the comparison's model is one of the two"
    assert tune.MODELS[MINI]["prices"]["input"] == "0.00000015" and tune.MODELS[MINI]["prices"]["output"] == "0.0000006"


def test_an_entry_keeps_the_version_its_text_the_model_and_each_tasks_result():
    item = _item("general-v2", MINI, ["a", "b"], ["c"])
    assert item["provider"] == "openai" and item["passed"] == ["a", "b"] and item["failed"] == ["c"]
    assert item["instructions_sha256"] == tune.instructions_hash("text of general-v2")
    assert item["cost_usd"] == "0.02", "an unavailable cost is not counted as a number"


def test_the_log_is_appended_and_refuses_a_changed_text_under_a_logged_version(tmp_path):
    tune.record(tmp_path, _item("general-v1", HAIKU, ["a"]))
    tune.record(tmp_path, _item("general-v1", MINI, ["a"]))
    with pytest.raises(ValueError, match="new version"):
        tune.record(tmp_path, _item("general-v1", MINI, ["a"], text="an edited text"))
    log = json.loads((tmp_path / tune.LOG).read_text(encoding="utf-8"))
    assert [(i["version"], i["model"]) for i in log] == [("general-v1", HAIKU), ("general-v1", MINI)]
    assert "general-v1" in (tmp_path / "tuning-log.md").read_text(encoding="utf-8")


def _log(*items):
    return list(items)


def test_a_version_holds_when_it_keeps_every_task_its_predecessor_passed_on_both_models():
    log = _log(_item("general-v1", HAIKU, ["a", "b"], ["c"]), _item("general-v1", MINI, ["a"], ["b", "c"]),
               _item("general-v2", HAIKU, ["a", "b", "c"]), _item("general-v2", MINI, ["a", "b"], ["c"]))
    found = tune.verdict(log, "general-v2")
    assert found == {"version": "general-v2", "predecessor": "general-v1", "holds": True, "lost": [],
                     "missing": [], "stop": True}


def test_a_version_that_loses_a_task_on_either_model_does_not_hold():
    log = _log(_item("general-v1", HAIKU, ["a", "b"]), _item("general-v1", MINI, ["a"], ["b"]),
               _item("general-v2", HAIKU, ["a", "b", "c"]), _item("general-v2", MINI, ["b"], ["a"]))
    found = tune.verdict(log, "general-v2")
    assert found["holds"] is False and found["lost"] == [f"a on {MINI}"] and found["stop"] is False


def test_a_version_run_on_one_model_only_does_not_hold():
    log = _log(_item("general-v1", HAIKU, ["a"]), _item("general-v1", MINI, ["a"]), _item("general-v2", HAIKU, ["a"]))
    found = tune.verdict(log, "general-v2")
    assert found["holds"] is False and found["missing"] == [MINI]


def test_tuning_stops_at_the_third_version_after_general_v1_held_or_not():
    log = []
    for number in range(1, 5):
        version = f"general-v{number}"
        # On Haiku each version passes the one task its predecessor failed: none holds.
        passed, failed = ("a", "b") if number % 2 else ("b", "a")
        log += [_item(version, HAIKU, [passed], [failed]), _item(version, MINI, ["c"])]
    assert [tune.verdict(log, f"general-v{n}")["stop"] for n in range(2, 5)] == [False, False, True]
    assert not any(tune.verdict(log, f"general-v{n}")["holds"] for n in range(2, 5))
    assert tune.verdict(log, "general-v1")["stop"] is False
    with pytest.raises(ValueError):
        tune.verdict(log, "general-v9")


def test_the_markdown_says_what_each_version_lost_and_what_it_failed(tmp_path):
    tune.record(tmp_path, _item("general-v1", HAIKU, ["a", "b"]))
    tune.record(tmp_path, _item("general-v1", MINI, ["a"], ["b"]))
    tune.record(tmp_path, _item("general-v2", HAIKU, ["b"], ["a"]))
    text = (tmp_path / "tuning-log.md").read_text(encoding="utf-8")
    assert "## general-v2 against general-v1: does not hold" in text
    assert f"- Lost: a on {HAIKU}" in text and f"- Not run yet: {MINI}" in text
    assert f"- {MINI}: b" in text


class _Arm:
    """A stand-in for this SDK's arm: each task's good outcome, a fetch shown."""

    name = "agentsdk"
    version = f"agentsdk test, preset {GENERAL_VERSION}"
    wire_format = "scripted"
    image = None

    def __init__(self, failing=()):
        self.failing = set(failing)
        self.seen = []

    async def run_sandbox(self, task):
        self.seen.append(task.id)
        changes = task.bad if task.id in self.failing else task.good
        return SandboxOutcome(answer=None, status="completed", turns=1, usage=Usage(prompt_tokens=10), wall_ms=1.0,
                              files=applied(task.files, changes), fetched=frozenset({FETCH_NAME}))


def test_a_tuning_run_logs_the_shipped_version_on_its_model_and_keeps_its_report(tmp_path):
    from agentsdk.registry import ModelPricing

    tasks = [BY_ID["count-error-lines"], BY_ID["rename-notes"], BY_ID["fetch-page"]]
    arm = _Arm(failing={"rename-notes"})
    item = tune.tune(arm=arm, model=MINI, tasks=tasks, pricing=ModelPricing(**tune.MODELS[MINI]["prices"]),
                     directory=tmp_path, verifier=None, witness=(_PAGE, "2026-11-08T00:00:00+00:00"),
                     stamp="2026-11-08T00:00:00+00:00")
    assert arm.seen == [t.id for t in tasks], "each task once"
    assert item["version"] == GENERAL_VERSION and item["model"] == MINI
    assert item["instructions_sha256"] == tune.instructions_hash(GENERAL_INSTRUCTIONS)
    assert item["passed"] == ["count-error-lines", "fetch-page"] and item["failed"] == ["rename-notes"]
    report = tmp_path / item["report"]
    assert report.is_file() and json.loads(report.read_text(encoding="utf-8"))["metadata"]["gateway_model"] == MINI
    assert tune.read_log(tmp_path) == [item]


def test_the_command_refuses_without_the_gateway_and_takes_only_the_two_models(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    assert tune.main(["--model", MINI, "--directory", str(tmp_path)]) == 2
    assert "BASE_URL and MODEL_API_KEY" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        tune.main(["--model", "openai.gpt-4o"])
    assert not (tmp_path / tune.LOG).exists()


def test_a_changed_text_under_a_logged_version_is_refused_before_anything_runs(tmp_path, monkeypatch):
    """Pre-review F4: the refusal comes before the spend, not after it."""
    from agentsdk.registry import ModelPricing

    tune.record(tmp_path, _item(GENERAL_VERSION, HAIKU, ["a"], text="an earlier text of this version"))
    arm = _Arm()
    with pytest.raises(ValueError, match="new version"):
        tune.tune(arm=arm, model=MINI, tasks=[BY_ID["count-error-lines"]],
                  pricing=ModelPricing(**tune.MODELS[MINI]["prices"]), directory=tmp_path, verifier=None,
                  witness=None)
    assert arm.seen == [], "the arm ran, and spent, before the refusal"


def test_two_runs_logging_at_once_both_keep_their_entry_and_leave_no_partial_file(tmp_path):
    """Pre-review F4: the log is read, appended and replaced under a lock, whole or not at all."""
    import threading

    items = [_item("general-v1", model, ["a"]) for model in (HAIKU, MINI) for _ in range(5)]
    threads = [threading.Thread(target=tune.record, args=(tmp_path, item)) for item in items]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(tune.read_log(tmp_path)) == len(items)
    assert sorted(p.name for p in tmp_path.iterdir()) == [tune.LOG, "tuning-log.md"], "a lock or partial file was left"


def test_a_crashed_runs_lock_is_taken_over_and_a_live_one_waited_for(tmp_path):
    import os
    import time

    lock = tmp_path / "tuning-log.lock"
    lock.write_text("")
    os.utime(lock, (time.time() - 7200, time.time() - 7200))
    tune.record(tmp_path, _item("general-v1", HAIKU, ["a"]))
    assert len(tune.read_log(tmp_path)) == 1 and not lock.exists()
    lock.write_text("")
    with pytest.raises(TimeoutError):
        with tune.locked(tmp_path, wait_seconds=0.5):
            pass
    assert lock.exists(), "another run's lock was removed"
