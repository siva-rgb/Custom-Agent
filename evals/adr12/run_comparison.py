"""Run the ADR-12 comparison live, against the gateway and the pinned Claude Code CLI.

This and tune.py are the only entry points that spend tokens. The gate's tests never call it: they run the
same harness with scripted stand-ins on both arms (AC-48, FR-180).

    python evals/adr12/run_comparison.py --repetitions 3                   # every task
    python evals/adr12/run_comparison.py --set sandbox --repetitions 1     # the sandbox tasks only
    python evals/adr12/run_comparison.py --only fetch-page --repetitions 1 # one task, as a smoke run

It needs BASE_URL and MODEL_API_KEY in the environment or in .env. The M15 tasks need the Claude
Code CLI installed beside this file (npm install --prefix evals/adr12); the sandbox tasks need
Docker and the Claude arm's image, built locally by the owner from evals/adr12/Dockerfile (FR-179):
this command never pulls or builds it. The report lands in evals/adr12/reports/, which is not
gitignored, so the credential scan reads it (AC-50).
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
# FR-179, DECISION-d0269633: this SDK's workspace image, pinned by digest, and the Claude arm's base.
WORKSPACE_IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"

# The prices this comparison costs both arms with. The SDK ships no price list (FR-30), and a
# stale bundled price would report a wrong number rather than an unknown one, so they are named
# here, in the report, and nowhere else.
PRICES = {"input": "0.000001", "output": "0.000005", "cache_read": "0.0000001", "cache_write": "0.00000125"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repetitions", type=int, default=3, help="runs per task and arm (P2-D13: 3)")
    parser.add_argument("--reports-dir", default=str(HERE / "reports"))
    parser.add_argument("--set", choices=("all", "m15", "sandbox"), default="all", dest="which",
                        help="the M15 tasks, the sandbox tasks, or both")
    parser.add_argument("--only", default=None, help="run one task by id, for a smoke run")
    arguments = parser.parse_args(argv)
    found = credentials("the live comparison")
    if found is None:
        return 2
    base_url, api_key = found

    from .arms import ClaudeAgentSdkArm, ThisSdkArm
    from .harness import run_comparison
    from .sandbox import SANDBOX_TASKS
    from .tasks import TASKS

    def chosen(found):
        return [task for task in found if arguments.only is None or task.id == arguments.only]

    tasks = chosen(TASKS) if arguments.which in ("all", "m15") else []
    sandbox_tasks = chosen(SANDBOX_TASKS) if arguments.which in ("all", "sandbox") else []
    if not tasks and not sandbox_tasks:
        print(f"no task with id {arguments.only!r} in the set {arguments.which}", file=sys.stderr)
        return 2
    if tasks and not CLI.is_file():
        print(f"the Claude Code CLI is not installed: run npm install --prefix {HERE.relative_to(REPO)}", file=sys.stderr)
        return 2

    pricing, registry, client = gateway(GATEWAY_MODEL, PRICES, 200_000, base_url, api_key)

    this_arm = ThisSdkArm(client_factory=client, model=GATEWAY_MODEL, model_registry=registry, budgets=True)
    claude_arm = ClaudeAgentSdkArm(cli_path=CLI, model=GATEWAY_MODEL, base_url=base_url, auth_token=api_key)
    sandbox_arms, images, verifier, witness, sweeping = [], {}, None, None, None
    if sandbox_tasks:
        import asyncio

        from agentsdk.workspace.docker import DockerProvider

        from .sandbox_arms import CLAUDE_IMAGE, ClaudeContainerArm, DockerCli, PresetArm, Verifier, fetch_witness

        claude_image = asyncio.run(_image_id(DockerCli(), CLAUDE_IMAGE))
        if claude_image is None:
            print(f"the Claude arm's image {CLAUDE_IMAGE} is not built here; the owner builds it with\n"
                  f"  docker build -t {CLAUDE_IMAGE} {HERE.relative_to(REPO).as_posix()}", file=sys.stderr)
            return 2
        images = {"agentsdk": WORKSPACE_IMAGE, "claude-agent-sdk": f"{CLAUDE_IMAGE} {claude_image}"}
        sandbox_arms = [
            PresetArm(client_factory=client, model=GATEWAY_MODEL, registry=registry, image=WORKSPACE_IMAGE,
                      provider=DockerProvider()),
            ClaudeContainerArm(image=CLAUDE_IMAGE, model=GATEWAY_MODEL, base_url=base_url, auth_token=api_key),
        ]
        # DECISION-b944d3ab: code tasks verified in a fresh container of the workspace image; fetch
        # tasks judged against the page the harness fetches itself; every comparison ends swept.
        from .sandbox_arms import sweep

        verifier = Verifier(image=WORKSPACE_IMAGE)
        if any(task.group == "fetch" for task in sandbox_tasks):
            witness = fetch_witness()
        sweeping = lambda: sweep(DockerCli())  # noqa: E731

    with tempfile.TemporaryDirectory(prefix="adr12-live-") as workdir:
        comparison = run_comparison(
            arms=[this_arm, claude_arm] if tasks else [], tasks=tasks, repetitions=arguments.repetitions,
            pricing=pricing, reports_dir=pathlib.Path(arguments.reports_dir), workdir=pathlib.Path(workdir),
            gateway_model=GATEWAY_MODEL, sandbox_arms=sandbox_arms, sandbox_tasks=sandbox_tasks,
            image_digests=images, verifier=verifier, witness=witness, sweep=sweeping,
        )
    passed = sum(1 for row in comparison.rows if row["passed"])
    print(f"{len(comparison.rows)} runs, {passed} checkers passed")
    print(f"wrote {comparison.json_path}")
    print(f"wrote {comparison.markdown_path}")
    return 0


def credentials(purpose: str) -> tuple[str, str] | None:
    """The gateway's address and key from the environment or the .env of the folder this is run
    from; None, having said which is missing, when either is."""
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
        print(f"set {' and '.join(missing)} in the environment or in .env to run {purpose}", file=sys.stderr)
        return None
    return base_url, api_key


def gateway(model: str, prices: dict[str, str], window: int, base_url: str, api_key: str):
    """The pricing, a registry naming `model` with its context window and prices, and a client
    factory for it through the gateway."""
    from agentsdk.providers import OpenAICompatibleModelClient
    from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry

    pricing = ModelPricing(**prices)
    registry = ModelRegistry([
        ModelEntry(provider="gateway", model_id=model, model_version="unknown", adapter_version="openai/1",
                   capabilities=ModelCapabilities(max_context_tokens=window, pricing=pricing))
    ])

    def client():
        return OpenAICompatibleModelClient(base_url=base_url, api_key=api_key, model=model)

    return pricing, registry, client


async def _image_id(docker, image: str) -> str | None:
    """The local image's id, or None when it is not built here: `image inspect` never pulls."""
    code, out, _ = await docker(["image", "inspect", "--format", "{{.Id}}", image])
    return out.decode().strip() if code == 0 and out.strip() else None


if __name__ == "__main__":
    if __package__ in (None, ""):  # run as a file, not as a module
        sys.path.insert(0, str(REPO))
        __package__ = "evals.adr12"
    raise SystemExit(main())
