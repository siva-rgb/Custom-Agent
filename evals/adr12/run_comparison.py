"""Run the ADR-12 comparison live, against the gateway and the pinned Claude Code CLI.

This is the only entry point that spends tokens. The gate's tests never call it: they run the
same harness with scripted stand-ins on both arms (AC-48).

    python evals/adr12/run_comparison.py --repetitions 3

It needs BASE_URL and MODEL_API_KEY in the environment or in .env, and the Claude Code CLI
installed beside this file (npm install --prefix evals/adr12). The report lands in
evals/adr12/reports/, which is not gitignored, so the credential scan reads it (AC-50).
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
GATEWAY_MODEL = "bedrock.anthropic.claude-haiku-4-5"
CLI = HERE / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"

# The prices this comparison costs both arms with. The SDK ships no price list (FR-30), and a
# stale bundled price would report a wrong number rather than an unknown one, so they are named
# here, in the report, and nowhere else.
PRICES = {"input": "0.000001", "output": "0.000005", "cache_read": "0.0000001", "cache_write": "0.00000125"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repetitions", type=int, default=3, help="runs per task and arm (P2-D13: 3)")
    parser.add_argument("--reports-dir", default=str(HERE / "reports"))
    parser.add_argument("--only", default=None, help="run one task by id, for a smoke run")
    arguments = parser.parse_args(argv)

    import os

    from dotenv import load_dotenv

    # Only a .env in the folder this is run from. load_dotenv() searches upward from this
    # file instead, which is inside the repository, so it would find the repository's .env
    # however it was invoked -- and a command meant to refuse without configuration would
    # quietly run a live comparison.
    load_dotenv(pathlib.Path.cwd() / ".env")

    base_url, api_key = (os.environ.get("BASE_URL") or "").strip(), (os.environ.get("MODEL_API_KEY") or "").strip()
    missing = [name for name, value in (("BASE_URL", base_url), ("MODEL_API_KEY", api_key)) if not value]
    if missing:
        print(f"set {' and '.join(missing)} in the environment or in .env to run the live comparison", file=sys.stderr)
        return 2
    if not CLI.is_file():
        print(f"the Claude Code CLI is not installed: run npm install --prefix {HERE.relative_to(REPO)}", file=sys.stderr)
        return 2

    from agentsdk.providers import OpenAICompatibleModelClient
    from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry

    from .arms import ClaudeAgentSdkArm, ThisSdkArm
    from .harness import run_comparison
    from .tasks import TASKS

    pricing = ModelPricing(**PRICES)
    registry = ModelRegistry([
        ModelEntry(provider="gateway", model_id=GATEWAY_MODEL, model_version="unknown", adapter_version="openai/1",
                   capabilities=ModelCapabilities(max_context_tokens=200_000, pricing=pricing))
    ])
    tasks = [task for task in TASKS if arguments.only is None or task.id == arguments.only]
    if not tasks:
        print(f"no task with id {arguments.only!r}", file=sys.stderr)
        return 2

    this_arm = ThisSdkArm(
        client_factory=lambda: OpenAICompatibleModelClient(base_url=base_url, api_key=api_key, model=GATEWAY_MODEL),
        model=GATEWAY_MODEL,
        model_registry=registry,
    )
    claude_arm = ClaudeAgentSdkArm(cli_path=CLI, model=GATEWAY_MODEL, base_url=base_url, auth_token=api_key)

    with tempfile.TemporaryDirectory(prefix="adr12-live-") as workdir:
        comparison = run_comparison(
            arms=[this_arm, claude_arm], tasks=tasks, repetitions=arguments.repetitions, pricing=pricing,
            reports_dir=pathlib.Path(arguments.reports_dir), workdir=pathlib.Path(workdir),
            gateway_model=GATEWAY_MODEL,
        )
    passed = sum(1 for row in comparison.rows if row["passed"])
    print(f"{len(comparison.rows)} runs, {passed} checkers passed")
    print(f"wrote {comparison.json_path}")
    print(f"wrote {comparison.markdown_path}")
    return 0


if __name__ == "__main__":
    if __package__ in (None, ""):  # run as a file, not as a module
        sys.path.insert(0, str(REPO))
        __package__ = "evals.adr12"
    raise SystemExit(main())
