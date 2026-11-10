"""Phase 5A: a ready general agent built on the SDK's governance (FR-174 to FR-177, DECISION-dd87feb8).

`general_agent(...)` returns a `GeneralAgent`: the spec of an agent that works in its sandboxed
workspace -- files, shell and code in the hardened container, the network only by the workspace's
`egress` -- and `config()`, a `RunConfig` that always carries a budget, 40 turns and a context
policy (P5A-D5 to P5A-D9). `general_subagent(...)` is the same agent as an orchestrator's role: a
briefed child, its budget its lease, its turns its briefing's (P5A-D11).

The instructions ship here, versioned: a changed text is a new version and a new `agent_spec_hash`,
and the version is recorded in the manifest's `preset` (FR-176). They name no provider, model or
provider-specific feature (NFR-52). Nothing changes for a caller who does not use this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .api import AgentSpec, RunConfig
from .budget import BudgetPolicy
from .context_policy import ContextPolicy
from .registry import ModelRegistry, default_registry
from .tools import ApprovalPolicy
from .workspace import _LIMITS, WorkspaceSpec

__all__ = [
    "GENERAL_INSTRUCTIONS",
    "GENERAL_TOOLS",
    "GENERAL_VERSION",
    "GeneralAgent",
    "SUBAGENT_INSTRUCTIONS",
    "SUBAGENT_TURNS",
    "SUBAGENT_VERSION",
    "general_agent",
    "general_subagent",
]

GENERAL, GENERAL_VERSION = "general", "general-v5"
SUBAGENT, SUBAGENT_VERSION = "general-subagent", "general-subagent-v1"
# What a caller's own text is recorded as: not a shipped version (FR-176).
CUSTOM = "custom"
# P5A-D6: the workspace tools, and nothing on the host.
GENERAL_TOOLS = ("run_command", "read_workspace_file", "write_workspace_file", "list_workspace",
                 "export_workspace_file")
# FR-177: the turns the documentation gives the general subagent's role through role_turns.
SUBAGENT_TURNS = 20

# general-v5 (M52, FR-186): general-v4's text with the delegate_task paragraph, which names the tool "when you
# have it" so a run without the tool reads instructions that still hold.
# general-v4: tuned in M51 on the comparison's 24 sandbox tasks (FR-182), its tuning log kept with that
# comparison; general-v1 to general-v3 are in the history. Shipped because the tuning rule stops at the
# third version after general-v1, not because it scored best: across both providers general-v2 passed
# one task more. Its text is general-v2's with three rules added: independent calls made in one turn,
# a download done another way when one way fails, and no result written that was not obtained.
GENERAL_INSTRUCTIONS = """\
You are a general-purpose agent. You work in a private workspace: a sandboxed Linux folder where you \
can read and write files and run programs. Nothing you do there touches the user's own machine.

Your tools:
- list_workspace and read_workspace_file show you what is already there. Start by looking: the task \
may come with files.
- write_workspace_file creates or replaces a file.
- run_command runs a program with its arguments, without a shell unless you name one (for example \
["sh", "-c", "..."]). It returns the exit code and the output. Use it to run code you wrote, to test \
it, and to inspect results.
- export_workspace_file saves one file of the workspace as an artifact for the user. It is the only \
way a file leaves the workspace, and it may wait for a person to approve it. Export only what the \
task asks you to deliver.
- delegate_task, when you have it, hands one self-contained piece of the task to a helper that works \
in a workspace of its own and answers you with text. Give it an objective that says exactly what to do \
and what to answer, and the paths of the files it needs. Delegate only work that stands on its own, \
such as independent parts done at the same time, and check what a helper answers before you rely on it.

How to work:
1. Read the task and any files it refers to before acting.
2. Prefer writing a small program and running it over working a result out in your head: computed \
answers are checked answers.
3. After each command, read its exit code and output. If something failed, find out why and fix it \
rather than repeating it unchanged.
4. Not every program is installed, and one may fail. Then do the same work another way, such as a \
short program in the language you are writing code in, rather than giving up.
5. Leave the files the task gave you as they are unless the task asks you to change them. Put your \
own tests and scratch work in new files.
6. Check your result before you answer: run it again and test it on cases beyond the examples you \
were given, such as empty input, repeated items, mixed case, surrounding spaces and boundary values.
7. The network is closed except for the hosts the task's workspace allows, and you are not told \
which those are. When the task asks you to fetch a URL, try it: an allowed host is reached through \
the proxy your environment already names (HTTPS_PROXY), and Python's urllib uses it. If one way of \
downloading fails, try another before you conclude the host is unreachable. Do not try any other host.
8. Never write a result you did not obtain: if a download or a computation fails, say so instead of \
writing what you expect it would have given.
9. Your turns and your budget are limited, and every turn resends the conversation. Make the calls \
that do not depend on each other in the same turn, read only what you need, do not repeat work you \
have already done, and stop when the task is done.

When you finish, answer with the result itself, briefly: what you found or built, where it is in \
the workspace, and anything that did not work. Do not claim a check you did not run.
"""

# general-subagent-v1
SUBAGENT_INSTRUCTIONS = """\
You are a general-purpose worker given one task by a coordinating agent. Your task and any inputs \
are in the briefing you were given; you cannot ask questions, so work from what it says. You work \
in a private workspace of your own: a sandboxed Linux folder where you can read and write files and \
run programs.

Your tools:
- list_workspace and read_workspace_file show you what is already there.
- write_workspace_file creates or replaces a file.
- run_command runs a program with its arguments, without a shell unless you name one (for example \
["sh", "-c", "..."]). It returns the exit code and the output.
- export_workspace_file saves one file as an artifact, and may wait for a person to approve it. \
Export only what your task asks you to deliver.

How to work:
1. Do the task you were briefed with, and only that task.
2. Prefer writing and running a small program over working a result out in your head, and check \
each command's exit code and output.
3. The network is closed unless your workspace allows a host. Do not try to reach anything else.
4. You have few turns. Do not repeat work, and stop as soon as the task is done.

Your answer goes back to the coordinating agent, not to a person. Answer with the result itself, \
in the form the briefing asks for, and say plainly if any part of the task could not be done. Do \
not claim a check you did not run.
"""


@dataclass(frozen=True)
class GeneralAgent:
    """FR-174: a general agent's spec, and the run configuration it always runs with."""

    spec: AgentSpec
    budget: BudgetPolicy
    max_turns: int
    context_policy: ContextPolicy

    def config(self, tenant_id: str, project_id: str, **overrides: Any) -> RunConfig:
        """A `RunConfig` carrying the preset's budget, turns and context policy; `overrides`
        replace any field of it."""
        fields: dict[str, Any] = {"budget": self.budget, "max_turns": self.max_turns,
                                  "context_policy": self.context_policy}
        fields.update(overrides)
        return RunConfig(tenant_id=tenant_id, project_id=project_id, **fields)


def _known_window(model: Any, registry: ModelRegistry | None) -> None:
    """P5A-D9, FR-78: a model whose context window the registry knows, or ValueError. `model` is
    a registry id, bare or after a `<client>:` prefix, as `preferred_model` takes it."""
    if type(model) is not str or not model:
        raise ValueError(f"model must be a non-empty registry id, got {model!r}")
    if registry is not None and not isinstance(registry, ModelRegistry):
        raise ValueError(f"registry must be a ModelRegistry or None, got {type(registry).__name__}")
    # Pre-review F3: split at the first colon, as the Runner resolves a reference (Runner._resolve_model).
    client, colon, model_id = model.partition(":")
    if colon and not client:
        raise ValueError(f"model {model!r} names an empty client before its colon")
    found = (registry if registry is not None else default_registry()).resolve(model_id if colon else model)
    if found is None or found.capabilities.max_context_tokens is None:
        raise ValueError(f"model {model!r}: the registry does not know its context window; "
                         "pass the registry your Runner registers it in")


def _workspace(image: str, egress: Any, files: Any, keep: bool, run_command_approval: ApprovalPolicy,
               export_approval: ApprovalPolicy, limits: Mapping[str, Any]) -> WorkspaceSpec:
    unknown = sorted(set(limits) - set(_LIMITS))
    if unknown:
        raise TypeError(f"unexpected arguments: {unknown}; the workspace's limits are {sorted(_LIMITS)}")
    if not isinstance(egress, (tuple, list)):
        raise ValueError(f"egress must be a tuple of 'host:port' pairs, got {type(egress).__name__}")
    return WorkspaceSpec(image=image, egress=tuple(egress), files=files if files is not None else {}, keep=keep,
                         run_command_approval=run_command_approval, export_approval=export_approval, **limits)


# Each shipped version's text, by (name, version).
SHIPPED = {(GENERAL, GENERAL_VERSION): GENERAL_INSTRUCTIONS, (SUBAGENT, SUBAGENT_VERSION): SUBAGENT_INSTRUCTIONS}


def recorded_preset(preset: tuple[str, str] | None, instructions: str) -> dict[str, str] | None:
    """FR-176, pre-review F4: what the manifest records for a spec's mark. A mark naming a shipped
    version whose text the spec no longer holds -- a spec changed after the factory made it -- is
    recorded as `custom`, so a recorded shipped version always names the text that ran."""
    if preset is None:
        return None
    name, version = preset
    shipped = SHIPPED.get((name, version))
    return {"name": name, "version": CUSTOM if shipped is not None and shipped != instructions else version}


def _version(instructions: str | None, shipped: str, version: str) -> tuple[str, str]:
    return (shipped, version) if instructions is None or instructions == shipped else (instructions, CUSTOM)


def general_agent(
    *,
    model: str,
    image: str,
    budget: BudgetPolicy,
    registry: ModelRegistry | None = None,
    egress: tuple[str, ...] = (),
    files: Mapping[str, bytes] | None = None,
    keep: bool = False,
    max_turns: int = 40,
    context_policy: ContextPolicy | None = None,
    run_command_approval: ApprovalPolicy = ApprovalPolicy.AUTO,
    export_approval: ApprovalPolicy = ApprovalPolicy.REQUIRE_APPROVAL,
    instructions: str | None = None,
    delegate: bool = False,
    delegate_approval: ApprovalPolicy = ApprovalPolicy.AUTO,
    **limits: Any,
) -> GeneralAgent:
    """FR-174: a general agent on `model`, working in a workspace made from `image`, always within
    `budget`. `limits` are the workspace's (`memory_bytes`, `cpus`, ...). Refused, ValueError,
    before anything is made: a model whose context window `registry` does not know, a budget that
    is no `BudgetPolicy`, and an image not pinned by digest.

    FR-186: `delegate=True` adds `delegate_task`, whose children are the general subagent on the same
    model, image, egress and limits, briefed with `SUBAGENT_TURNS`; `delegate_approval` decides
    whether each spawn waits for a person."""
    _known_window(model, registry)
    if not isinstance(budget, BudgetPolicy):
        raise ValueError(f"budget must be a BudgetPolicy, got {type(budget).__name__}")
    if type(delegate) is not bool:
        raise ValueError(f"delegate must be True or False, got {delegate!r}")
    space = _workspace(image, egress, files, keep, run_command_approval, export_approval, limits)
    text, version = _version(instructions, GENERAL_INSTRUCTIONS, GENERAL_VERSION)
    policy = context_policy if context_policy is not None else ContextPolicy()
    # Checked now, as a run would check them, so a bad value fails here rather than at config().
    RunConfig(tenant_id="-", project_id="-", budget=budget, max_turns=max_turns, context_policy=policy)
    delegation = None
    if delegate:
        from .delegation import DELEGATE_TOOL, Delegation

        child = general_subagent(model=model, image=image, registry=registry, egress=egress,
                                 run_command_approval=run_command_approval, export_approval=export_approval,
                                 **limits)
        delegation = Delegation(agent=child, max_turns=SUBAGENT_TURNS, approval=delegate_approval)
    tools = (*GENERAL_TOOLS, DELEGATE_TOOL) if delegate else GENERAL_TOOLS
    spec = AgentSpec(id=GENERAL, instructions=text, preferred_model=model, tool_profile=tools,
                     workspace=space, preset=(GENERAL, version), delegate=delegation)
    return GeneralAgent(spec=spec, budget=budget, max_turns=max_turns, context_policy=policy)


def general_subagent(
    *,
    model: str,
    image: str,
    registry: ModelRegistry | None = None,
    egress: tuple[str, ...] = (),
    files: Mapping[str, bytes] | None = None,
    run_command_approval: ApprovalPolicy = ApprovalPolicy.AUTO,
    export_approval: ApprovalPolicy = ApprovalPolicy.REQUIRE_APPROVAL,
    instructions: str | None = None,
    **limits: Any,
) -> AgentSpec:
    """FR-177: the general agent as an orchestrator's role. A child's budget is its lease, its turns
    its briefing's -- give its role `role_turns={role: SUBAGENT_TURNS}` -- and its context policy
    the orchestrator's, so none is taken here. Each child gets a workspace of its own."""
    _known_window(model, registry)
    space = _workspace(image, egress, files, False, run_command_approval, export_approval, limits)
    text, version = _version(instructions, SUBAGENT_INSTRUCTIONS, SUBAGENT_VERSION)
    return AgentSpec(id=SUBAGENT, instructions=text, preferred_model=model, tool_profile=GENERAL_TOOLS,
                     workspace=space, preset=(SUBAGENT, version))
