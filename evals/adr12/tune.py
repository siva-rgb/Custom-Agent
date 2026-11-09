"""Tune the general preset between comparisons (FR-182, P5A-D10, DECISION-7830ca10).

Tuning changes the preset's instructions alone, as SDK source: each change is a new version
(general-v2, ...) and a new agent_spec_hash. Each version runs the 24 sandbox tasks once on this
SDK's arm, on each of two upstream providers through the gateway, and is recorded in the tuning log
with the providers it ran on, its instructions' hash, and each task's result. The Claude arm does
not run: tuning is of this SDK's preset alone.

    python evals/adr12/tune.py --model bedrock.anthropic.claude-haiku-4-5
    python evals/adr12/tune.py --model openai.gpt-4o-mini

Tuning stops at the first version that passes every sandbox task its predecessor passed, on both
providers, or at the third new version (general-v4); the final comparison (run_comparison.py) runs
the last version. Each tuning run's full report is kept under reports/tuning/, beside the log.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import time
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
TUNING_DIR = HERE / "reports" / "tuning"
LOG = "tuning-log.json"

# DECISION-7830ca10: the comparison's model, and a second upstream provider.
HAIKU, MINI = "bedrock.anthropic.claude-haiku-4-5", "openai.gpt-4o-mini"
# Each model's prices per token and its context window. Haiku 4.5's are the comparison's own
# (run_comparison.PRICES); gpt-4o-mini's are OpenAI's list prices, USD 0.15 and 0.60 per million
# input and output tokens, 0.075 cached (openai.com/api/pricing, read 2026-11-08). The gateway may
# bill otherwise; these cost the log, as the comparison's cost its report.
MODELS: dict[str, dict[str, Any]] = {
    HAIKU: {"prices": None, "window": 200_000},
    MINI: {"prices": {"input": "0.00000015", "output": "0.0000006", "cache_read": "0.000000075",
                      "cache_write": "0.00000015"}, "window": 128_000},
}
# DECISION-7830ca10: at most three versions after general-v1.
MAX_VERSIONS = 3


def instructions_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def entry(version: str, instructions: str, model: str, rows: list[dict[str, Any]], report: str,
          date: str) -> dict[str, Any]:
    """One version on one model: what the log keeps of a tuning run."""
    return {
        "version": version,
        "instructions_sha256": instructions_hash(instructions),
        "model": model,
        "provider": model.split(".", 1)[0],
        "date": date,
        "report": report,
        "passed": sorted(row["task"] for row in rows if row["passed"]),
        "failed": sorted(row["task"] for row in rows if not row["passed"]),
        "statuses": {row["task"]: row["status"] for row in rows},
        "cost_usd": str(sum(_decimal(row.get("cost_usd")) for row in rows)),
    }


def _decimal(value: Any):
    from decimal import Decimal, InvalidOperation

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def read_log(directory: pathlib.Path) -> list[dict[str, Any]]:
    path = pathlib.Path(directory) / LOG
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []


def refuse_changed_text(log: list[dict[str, Any]], version: str, digest: str) -> None:
    """A version's text is fixed once logged: the same version under another instructions hash is
    refused, since a changed text must be a new version (P5A-D10)."""
    for earlier in log:
        if earlier["version"] == version and earlier["instructions_sha256"] != digest:
            raise ValueError(f"{version} was logged with other instructions: a changed text is a new version")


@contextlib.contextmanager
def locked(directory: pathlib.Path, wait_seconds: float = 120.0):
    """The log's lock (pre-review F4): two tuning runs started together each append their entry. A
    lock older than an hour is a crashed run's, and is taken over."""
    lock = pathlib.Path(directory) / "tuning-log.lock"
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - lock.stat().st_mtime > 3600:
                    lock.unlink()
                    continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"{lock} is held by another tuning run") from None
            time.sleep(0.2)
    try:
        yield
    finally:
        os.close(handle)
        with contextlib.suppress(OSError):
            lock.unlink()


def _replace(path: pathlib.Path, text: str) -> None:
    """Writes `text` to `path` whole or not at all (pre-review F4)."""
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)


def record(directory: pathlib.Path, item: dict[str, Any]) -> list[dict[str, Any]]:
    """Append one tuning run to the log, and rewrite its Markdown view; the whole log returned. The
    text check is made again here, under the lock, though tune() makes it before spending."""
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with locked(directory):
        log = read_log(directory)
        refuse_changed_text(log, item["version"], item["instructions_sha256"])
        log.append(item)
        _replace(directory / LOG, json.dumps(log, indent=2, sort_keys=True))
        _replace(directory / "tuning-log.md", markdown(log))
    return log


def versions(log: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(item["version"] for item in log))


def latest(log: list[dict[str, Any]], version: str, model: str) -> dict[str, Any] | None:
    found = [item for item in log if item["version"] == version and item["model"] == model]
    return found[-1] if found else None


def verdict(log: list[dict[str, Any]], version: str) -> dict[str, Any]:
    """DECISION-7830ca10's stop rule for `version`: it holds when, on both models, it passes every
    task its predecessor passed. A version not yet run on both models does not hold."""
    order = versions(log)
    if version not in order:
        raise ValueError(f"{version} is not in the tuning log")
    index = order.index(version)
    lost, missing = [], [model for model in MODELS if latest(log, version, model) is None]
    if index == 0:
        return {"version": version, "predecessor": None, "holds": False, "lost": [], "missing": missing,
                "stop": False}
    predecessor = order[index - 1]
    missing += [f"{predecessor} on {model}" for model in MODELS if latest(log, predecessor, model) is None]
    for model in MODELS:
        now, before = latest(log, version, model), latest(log, predecessor, model)
        if now is not None and before is not None:
            lost += [f"{task} on {model}" for task in before["passed"] if task not in now["passed"]]
    holds = not missing and not lost
    return {"version": version, "predecessor": predecessor, "holds": holds, "lost": lost, "missing": missing,
            "stop": holds or index >= MAX_VERSIONS}


def markdown(log: list[dict[str, Any]]) -> str:
    lines = ["# Tuning the general preset (FR-182)", "",
             "Each version of the preset's instructions, on each model it ran on: the 24 sandbox tasks once, on "
             "this SDK's arm. DECISION-7830ca10: tuning stops at the first version that passes every task its "
             "predecessor passed on both models, or at the third version after general-v1.", "",
             "| version | instructions sha256 | model | date | passed | cost | report |", "|---|---|---|---|---|---|---|"]
    for item in log:
        lines.append(f"| {item['version']} | {item['instructions_sha256'][:12]} | {item['model']} | {item['date']} | "
                     f"{len(item['passed'])}/{len(item['passed']) + len(item['failed'])} | {item['cost_usd']} | "
                     f"{item['report']} |")
    for version in versions(log)[1:]:
        found = verdict(log, version)
        said = "holds" if found["holds"] else "does not hold"
        lines += ["", f"## {version} against {found['predecessor']}: {said}"]
        if found["lost"]:
            lines.append("- Lost: " + ", ".join(found["lost"]))
        if found["missing"]:
            lines.append("- Not run yet: " + ", ".join(found["missing"]))
    for version in versions(log):
        failing = {item["model"]: item["failed"] for item in log if item["version"] == version}
        lines += ["", f"## {version}: failed tasks"]
        lines += [f"- {model}: {', '.join(tasks) or 'none'}" for model, tasks in failing.items()]
    return "\n".join(lines) + "\n"


def tune(*, arm: Any, model: str, tasks: Any, pricing: Any, directory: pathlib.Path, verifier: Any,
         witness: Any, sweep: Any = None, stamp: str | None = None, image_digests: dict[str, str] | None = None):
    """Run the sandbox tasks once on `arm` with the preset as shipped, keep the run's report, and
    log it as the shipped version on `model`; the log's entry returned."""
    from agentsdk.presets import GENERAL_INSTRUCTIONS, GENERAL_VERSION

    from .harness import run_comparison

    directory = pathlib.Path(directory)
    # Pre-review F4: refused before any spend, not after it.
    refuse_changed_text(read_log(directory), GENERAL_VERSION, instructions_hash(GENERAL_INSTRUCTIONS))
    reports = directory / f"{GENERAL_VERSION}-{model}"
    with tempfile.TemporaryDirectory(prefix="adr12-tune-") as workdir:
        comparison = run_comparison(
            arms=[], tasks=[], repetitions=1, pricing=pricing, reports_dir=reports, workdir=pathlib.Path(workdir),
            gateway_model=model, stamp=stamp, sandbox_arms=[arm], sandbox_tasks=tasks,
            image_digests=image_digests, verifier=verifier, witness=witness, sweep=sweep,
        )
    if comparison.metadata.get("incomplete"):
        raise RuntimeError(f"the tuning run stopped early: {comparison.metadata['incomplete']}")
    item = entry(GENERAL_VERSION, GENERAL_INSTRUCTIONS, model, comparison.rows,
                 comparison.json_path.relative_to(directory).as_posix(), comparison.metadata["date"])
    record(directory, item)
    return item


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, choices=tuple(MODELS))
    parser.add_argument("--directory", default=str(TUNING_DIR))
    arguments = parser.parse_args(argv)

    from .run_comparison import GATEWAY_MODEL, PRICES, WORKSPACE_IMAGE, credentials, gateway

    found = credentials("a tuning run")
    if found is None:
        return 2
    model = arguments.model
    prices = MODELS[model]["prices"] or PRICES
    assert model != GATEWAY_MODEL or prices is PRICES
    pricing, registry, client = gateway(model, prices, MODELS[model]["window"], *found)

    from agentsdk.workspace.docker import DockerProvider

    from .sandbox import SANDBOX_TASKS
    from .sandbox_arms import DockerCli, PresetArm, Verifier, fetch_witness, sweep

    arm = PresetArm(client_factory=client, model=model, registry=registry, image=WORKSPACE_IMAGE,
                    provider=DockerProvider())
    item = tune(arm=arm, model=model, tasks=SANDBOX_TASKS, pricing=pricing, directory=pathlib.Path(arguments.directory),
                verifier=Verifier(image=WORKSPACE_IMAGE), witness=fetch_witness(),
                sweep=lambda: sweep(DockerCli()), image_digests={"agentsdk": WORKSPACE_IMAGE})
    print(f"{item['version']} on {model}: {len(item['passed'])} of {len(item['passed']) + len(item['failed'])} "
          f"passed, {item['cost_usd']} USD")
    print("failed: " + (", ".join(item["failed"]) or "none"))
    return 0


if __name__ == "__main__":
    if __package__ in (None, ""):  # run as a file, not as a module
        sys.path.insert(0, str(REPO))
        __package__ = "evals.adr12"
    raise SystemExit(main())
