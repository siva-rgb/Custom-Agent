"""45 - Increment 2's exit signal: delegating general agents of two tenancies, at once, inside their governance.

Phase 5A increment 2's exit signal (P5A-D4, FR-190). Two tenancies share one Runner and one project
name. Each starts five delegating general agents (`general_agent(..., delegate=True)`), in a shuffled
order with random delays:

  piece      delegates a piece of its work naming one of its three files; then makes a link and a
             file past the transfer's 16 MiB bound (FR-143), and delegates a path outside its
             workspace, the link, and the large file
  runaway    delegates work its child's reservation cannot pay for, then works on until its own
             ceiling stops it
  nester     delegates to a child that tries to delegate in turn
  asker      under REQUIRE_APPROVAL, asks for two spawns: its tenancy's operator approves the first
             and refuses the second
  canceller  delegates a long command; while the child runs it, the other tenancy tries to release
             and snapshot the child's workspace, and then the parent is cancelled

Each tenancy's operator decides its own requests for approval. When every run has ended, each
tenancy tries to reach the other's: the piece's kept workspace, its rows (runs' workspaces,
commands, interruptions) and its answers (a parent's and a child's history). Then `Runner.aclose()`,
after which nothing of either tenancy remains.

Every check names its subject and has a witness, independent of the property, that the subject
happened (KNOWLEDGE-1afcf6b2); a subject that never happened fails, in every mode (DECISION-a7d6a1f5).
Checks read the store, the provider and the commands' own output -- never an error's text
(DECISION-94bfee7c): a refused call is told by its error type and its spawning nothing. The hostile
attempts are calls the scripted parents and children make; live, the model is given each verbatim,
and one it declines fails its check (FR-190). The one exception is the nested child's
`delegate_task`: a child has no such tool, so a live model cannot call it, and the nester is
scripted in every mode, as example 44's is.

Run it
  python scripts/45_delegation_contention.py             # live: gateway + PostgreSQL + Docker
  python scripts/45_delegation_contention.py --offline   # in memory, scripted model, FakeProvider
  python scripts/45_delegation_contention.py --scripted  # in memory, scripted model, Docker

Live and --scripted need Docker running and the image below on the host:
  docker pull python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import random
import subprocess
import sys
import uuid
from decimal import Decimal

from agentsdk import Message, Role, RunStatus, Runner
from agentsdk.budget import BUDGET_EXCEEDED, BudgetPolicy
from agentsdk.delegation import DELEGATE_TOOL
from agentsdk.events import EventType
from agentsdk.identity import PrincipalContext
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import RunScope
from agentsdk.presets import general_agent
from agentsdk.primitives import ToolCall
from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry, default_registry
from agentsdk.tools import ApprovalPolicy
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceUnavailable
from agentsdk.workspace.manager import _KILL_SECONDS

KILL_MS = _KILL_SECONDS * 1000

TENANT = "example-tenant"
ELSEWHERE = "-elsewhere"  # the second tenancy: the tenant with this added
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
FAKE_IMAGE = "fake/workspace@sha256:" + "0" * 64
NL = chr(10)
NOT_FOUND = "urn:agentsdk:tool-error:ToolNotFound"
TOOL_RAISED = "ToolExecutionError"  # ToolCalled.error_type of any failure the tool itself raised
TERMINAL = (EventType.RUN_COMPLETED.value, EventType.RUN_FAILED.value, EventType.RUN_CANCELLED.value)


def operator_of(tenant: str) -> PrincipalContext:
    return PrincipalContext(agent_principal="example-45", user_principal=f"operator of {tenant}")


# --- the work, and the hostile calls ---------------------------------------------------------------

FILES = {"data/a.txt": b"17\n25\n", "data/b.txt": b"8\n50\n",
         "NOTES.md": b"data/a.txt and data/b.txt hold one whole number per line.\n"}
NAMED = "data/a.txt"
OUTSIDE = "../outside.txt"
LINK, LINK_TARGET = "passwd.link", "/etc/passwd"
BIG, BIG_MB = "big.bin", 17  # 17,000,000 bytes, past the transfer's 16 MiB (16,777,216 bytes)


def told(what: str, turns: list[list]) -> str:
    """A task that gives its calls verbatim: the live model is told to make exactly these, and the
    scripted one reads them from the TURNS line."""
    return (f"{what} Make exactly these tool calls, in this order, one turn each, copying every argument "
            f"verbatim; then answer in one sentence.{NL}TURNS: {json.dumps(turns)}")


def turns_of(text: str) -> list[list] | None:
    if "TURNS: " not in text:
        return None
    return json.loads(text[text.index("TURNS: ") + 7:].split(NL)[0])


def delegate(objective: str, files: list[str] | None = None) -> list:
    return [DELEGATE_TOOL, {"objective": objective, **({"files": files} if files is not None else {})}]


def command(*argv: str) -> list:
    return ["run_command", {"argv": list(argv)}]


def listing(path: str) -> list:
    return ["list_workspace", {"path": path}]


# Pre-review F2: the child lists its own workspace while it runs (it is removed when the child ends),
# so what it held is seen, not inferred from its record.
PIECE_CHILD = told("List your workspace, then read the numbers in data/a.txt.",
                   [[listing(".")], [listing("data")], [command("cat", NAMED)]])
PIECE = told("You are testing what a helper may be given.", [
    [delegate(PIECE_CHILD, [NAMED])],
    [command("ln", "-s", LINK_TARGET, LINK)],
    [command("dd", "if=/dev/zero", f"of={BIG}", "bs=1000000", f"count={BIG_MB}")],
    [delegate("Answer with the word outside.", [OUTSIDE])],
    [delegate("Answer with the word link.", [LINK])],
    [delegate("Answer with the word big.", [BIG])],
])
HOSTILE = {OUTSIDE: "a path outside the parent's workspace", LINK: "a link", BIG: "a file past the transfer's bound"}

RUNAWAY_TURNS = 20  # the child's documented turns (SUBAGENT_TURNS): its reservation stops it first
RUNAWAY_CHILD = told("Keep working until you are stopped.", [[command("echo", "tick")]] * RUNAWAY_TURNS)
RUNAWAY = told("Hand a helper its work, then keep working until you are stopped.",
               [[delegate(RUNAWAY_CHILD)], *[[command("echo", "tick")]] * 40])

NEST_CHILD = told("Try to delegate a task of your own.", [[delegate("Answer with the word nested.")]])
NESTER = told("Ask a helper to delegate.", [[delegate(NEST_CHILD)]])

HELLO, GOODBYE = "Answer with the word hello.", "Answer with the word goodbye."
ASKER = told("Ask two helpers, one after the other.", [[delegate(HELLO)], [delegate(GOODBYE)]])

SLEEP_SECONDS = "30"
SLEEPER = told("Run a long job.", [[command("sleep", SLEEP_SECONDS)]])
CANCELLER = told("Hand a helper a long job.", [[delegate(SLEEPER)]])

TASKS = {"piece": PIECE, "runaway": RUNAWAY, "nester": NESTER, "asker": ASKER, "canceller": CANCELLER}
NAMES = tuple(TASKS)


# --- the scripted model ---------------------------------------------------------------------------

def _reply(calls=(), content=""):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(calls)),
                         stop_reason=StopReason.TOOL_CALLS if calls else StopReason.END_TURN, usage=Usage(500, 50, 550))


class ScriptedModel:
    """Makes its task's calls, a turn at a time, after a random delay; then answers. A child's task is
    its briefing, which holds the objective its parent wrote."""

    def __init__(self, rng: random.Random, delay: float) -> None:
        self.rng, self.delay = rng, delay

    async def send(self, request):
        await asyncio.sleep(self.rng.random() * self.delay)
        task = next(m.content for m in request.messages if m.role is Role.USER)
        turns = turns_of(task) or []
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turn >= len(turns):
            return _reply(content="Done.")
        return _reply([ToolCall(id=f"t{turn}-{i}", name=name, arguments=arguments)
                       for i, (name, arguments) in enumerate(turns[turn])])


# Each scripted call costs USD 0.10: 500 tokens in at 0.0001, 50 out at 0.001.
SCRIPTED_PRICING = ModelPricing(input=Decimal("0.0001"), output=Decimal("0.001"))


def _entry(provider: str, model: str, pricing: ModelPricing) -> ModelEntry:
    return ModelEntry(provider=provider, model_id=model, model_version="1", adapter_version="scripted/1",
                      capabilities=ModelCapabilities(max_context_tokens=200_000, pricing=pricing))


def scripted_registry() -> ModelRegistry:
    return ModelRegistry([_entry("m", "scripted", SCRIPTED_PRICING), _entry("s", "scripted", SCRIPTED_PRICING)])


def live_registry() -> ModelRegistry:
    """The shipped models, and the scripted nester priced at nothing: it calls no provider."""
    free = ModelPricing(input=Decimal(0), output=Decimal(0))
    return ModelRegistry([*default_registry().entries(), _entry("s", "scripted", free)])


class RecordingRunner(Runner):
    """Keeps every run's event sink and configuration, so a run's events, parent and lease are read in
    memory as on PostgreSQL."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sinks: dict[str, object] = {}
        self.configs: dict[str, object] = {}
        self.reserved: dict[str, dict] = {}

    def _open(self, spec, config):
        opened = super()._open(spec, config)
        run_id = opened.scope.run_id
        self.sinks[run_id], self.configs[run_id] = opened.events, config
        lease = config.budget_lease
        if lease is not None:
            # As the manifest names it, at the run's start: a released lease shrinks to its spend.
            usd = lease.reservation.usd
            self.reserved[run_id] = {lease.node_id: {"usd": None if usd is None else str(usd),
                                                     "tokens": lease.reservation.tokens}}
        return opened

    def started_under(self, run_id: str) -> list[str]:
        return [r for r, config in self.configs.items() if config.parent_run_id == run_id]


# --- FakeProvider's stand-ins for the image's programs (--offline) ---------------------------------

async def _cat(cmd) -> int:
    if len(cmd.argv) != 2 or not cmd.files.exists(cmd.argv[1]):
        cmd.err(f"cat: {cmd.argv[1:]!r}: No such file or directory\n")
        return 1
    cmd.out(cmd.files.read(cmd.argv[1]).decode())
    return 0


async def _echo(cmd) -> int:
    cmd.out(" ".join(cmd.argv[1:]) + "\n")
    return 0


async def _ln(cmd) -> int:
    if len(cmd.argv) != 4 or cmd.argv[1] != "-s":
        cmd.err("ln: the fake makes only ln -s TARGET NAME\n")
        return 1
    cmd.files.link(cmd.argv[3], cmd.argv[2])
    return 0


async def _dd(cmd) -> int:
    given = dict(a.split("=", 1) for a in cmd.argv[1:] if "=" in a)
    if given.get("if") != "/dev/zero" or "of" not in given:
        cmd.err("dd: the fake writes only zeros\n")
        return 1
    cmd.files.write(given["of"], bytes(int(given.get("bs", "512")) * int(given.get("count", "1"))))
    return 0


async def _sleep(cmd) -> int:
    await cmd.sleep(float(cmd.argv[1]))
    return 0


def provider(name: str):
    if name == "docker":
        return DockerProvider()
    return FakeProvider(commands={"cat": _cat, "echo": _echo, "ln": _ln, "dd": _dd, "sleep": _sleep})


# --- reading what happened ------------------------------------------------------------------------

def _stored(sql: str, *args):
    import psycopg

    from agentsdk.config import normalise_database_url

    with psycopg.connect(normalise_database_url(os.environ["DATABASE_URL"])) as conn:
        return conn.execute(sql, args).fetchall()


async def events_of(runner: RecordingRunner, persistence, run_id: str) -> list[tuple[str, dict]]:
    if persistence is None:
        return [(e.event_type.value, e.payload) for e in runner.sinks[run_id].events()]
    rows = await asyncio.to_thread(_stored, "SELECT event_type, payload FROM run_events WHERE run_id = %s::uuid"
                                            " ORDER BY sequence_no", run_id)
    return [(kind, payload) for kind, payload in rows]


async def children_of(runner: RecordingRunner, persistence, run_id: str | None) -> list[str]:
    """The runs whose parent is `run_id`: from the runs table, or in memory from their configurations."""
    if run_id is None:
        return []
    if persistence is not None:
        rows = await asyncio.to_thread(_stored, "SELECT run_id::text FROM runs WHERE parent_run_id = %s::uuid"
                                                " ORDER BY started_at", run_id)
        return [r[0] for r in rows]
    return runner.started_under(run_id)


async def reservation_of(runner: RecordingRunner, persistence, run_id: str) -> tuple[list[str], Decimal | None]:
    """A child's reservations by name, from its manifest (in memory, the lease it ran on), and the
    USD of the one it ran on."""
    if persistence is not None:
        rows = await asyncio.to_thread(_stored, "SELECT budget_reservations FROM execution_manifests"
                                                " WHERE run_id = %s::uuid", run_id)
        held = (rows[0][0] if rows else None) or {}
    else:
        held = runner.reserved.get(run_id) or {}
    names = list(held)
    usd = (held[names[0]] or {}).get("usd") if len(names) == 1 else None
    return names, None if usd is None else Decimal(usd)


def costs(events) -> list[Decimal]:
    """Each priced model call's and command's cost (FR-157), in order."""
    return [Decimal(p["cost_usd"]) for k, p in events if k in (EventType.MODEL_CALLED.value,
                                                                EventType.WORKSPACE_COMMAND_ENDED.value)
            and p.get("cost_usd")]


def ending(events) -> tuple[str | None, str | None]:
    """A run's terminal status and reason, from its own last event; None while it runs."""
    found = [p for k, p in events if k in TERMINAL]
    return (found[-1].get("status"), found[-1].get("reason")) if found else (None, None)


def made(events) -> list[str]:
    return [p["workspace_id"] for k, p in events if k == EventType.WORKSPACE_CREATED.value]


async def history_of(runner: Runner, tenant: str, project: str, run_id: str):
    sessions = runner._sessions_for(RunScope(run_id=run_id, tenant_id=tenant, project_id=project))
    return await asyncio.to_thread(sessions.history, run_id)


async def calls_of(runner: Runner, tenant: str, project: str, run_id: str) -> list[tuple[ToolCall, object]]:
    """Each call the run's model made, with its result, in order."""
    history = await history_of(runner, tenant, project, run_id)
    results = {r.tool_call_id: r for m in history for r in m.tool_results}
    return [(call, results.get(call.id)) for m in history for call in m.tool_calls]


def called(events) -> dict[str, dict]:
    """Each ToolCalled of the run, by its call id."""
    return {p["tool_call_id"]: p for k, p in events if k == EventType.TOOL_CALLED.value and p.get("tool_call_id")}


def _docker(*args: str) -> list[str] | None:
    try:
        done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.split() if done.returncode == 0 else None


async def remains(chosen, tenant: str) -> list[str] | None:
    """Every container, volume and network labelled with the tenant, on Docker; on the fake, its
    listing and its snapshots. None when a listing failed."""
    if not isinstance(chosen, DockerProvider):
        spaces = [h.workspace_id for h in await chosen.list_owned({"agentsdk.tenant": tenant})]
        return spaces + [s.labels.get("agentsdk.snapshot", "?") for s in
                         await chosen.list_snapshots({"agentsdk.tenant": tenant})]
    found = []
    for listing in (["ps", "-aq"], ["volume", "ls", "-q"], ["network", "ls", "-q"]):
        names = await asyncio.to_thread(_docker, *listing, "--filter", f"label=agentsdk.tenant={tenant}")
        if names is None:
            return None
        found += names
    return found


async def reach(runner: Runner, chosen, tenant: str, project: str, workspace_id: str) -> tuple:
    """`tenant`'s attempts on a workspace: release it, snapshot it; and whether it is still there."""
    released = await runner.release_workspace(tenant, project, workspace_id)
    try:
        await runner.snapshot_workspace(tenant, project, workspace_id)
        snapped = "taken"
    except WorkspaceUnavailable:
        snapped = "unavailable"
    except Exception as exc:  # noqa: BLE001 - any other answer is not the refusal
        snapped = type(exc).__name__
    still = bool(await chosen.list_owned({"agentsdk.workspace": workspace_id}))
    return released, snapped, still


# --- the operators --------------------------------------------------------------------------------

async def operator(runner: RecordingRunner, tenant: str, project: str, asked: list) -> None:
    """Decides every request for approval of its tenancy as it is asked, noting first how many children
    the asking run had. A spawn whose objective is HELLO is approved; every other spawn, and any export
    a live model asks for, is refused, so nothing waits for a person. A decision that fails is tried
    again on the next pass."""
    noted: set[str] = set()
    principal = operator_of(tenant)
    while True:
        for view in await runner.interruptions(tenant, project):
            item = view.interruption
            arguments = item.request_payload.get("arguments") or {}
            approve = item.tool_name == DELEGATE_TOOL and arguments.get("objective") == HELLO
            if item.interruption_id not in noted:
                noted.add(item.interruption_id)
                asked.append((item.run_id, item.tool_name, arguments.get("objective"),
                              len(runner.started_under(item.run_id)), approve))
            try:
                await runner.decide(tenant, project, item.interruption_id, approve=approve, principal=principal)
            except Exception:  # noqa: BLE001 - tried again on the next pass, while it is pending
                pass
        await asyncio.sleep(0.05)


# --- the demonstration ----------------------------------------------------------------------------

class Tenancy:
    """What one tenancy started, and what it saw."""

    def __init__(self, tenant: str) -> None:
        self.tenant = tenant
        self.other: Tenancy | None = None
        self.runs: dict[str, str] = {}  # name -> run id
        self.results: dict[str, object] = {}
        self.ceilings: dict[str, Decimal] = {}
        self.asked: list = []
        # The canceller's child: its run and workspace, the other tenancy's attempts on that workspace
        # while it ran, and how it ended.
        self.sleeper: tuple[str, str] | None = None
        self.sleeper_reached: tuple | None = None
        self.sleeper_end: tuple | None = None
        # Round 1 F-B: what the tenancy's listing held just before it released its own kept workspace.
        self.held: list[str] | None = None


def slack(ceiling: Decimal, largest: Decimal, together: int, limit: int) -> Decimal:
    """A run's bound (P5A-D17, NFR-23): its ceiling and one call's or command's cost per agent running
    at once, which `max_concurrent_subagents` caps (round 1 F-C)."""
    return ceiling + largest * min(together, limit)


async def _sleeping_child(runner: RecordingRunner, persistence, run_id: str) -> tuple[str, str] | None:
    """The parent's child once its long command has started, with its workspace; None if the parent
    ends first."""
    for _ in range(3000):
        for kid in runner.started_under(run_id):
            said = await events_of(runner, persistence, kid)
            started = [p for k, p in said if k == EventType.WORKSPACE_COMMAND_STARTED.value
                       and (p.get("argv") or [None])[0] == "sleep"]
            if started:
                return kid, started[0]["workspace_id"]
        if ending(await events_of(runner, persistence, run_id))[0] is not None:
            return None
        await asyncio.sleep(0.1)
    return None


async def _ended(runner: RecordingRunner, persistence, run_id: str, seconds: float) -> tuple:
    """A run's terminal status and reason once it has one, waiting at most `seconds`."""
    for _ in range(int(seconds * 10)):
        found = ending(await events_of(runner, persistence, run_id))
        if found[0] is not None:
            return found
        await asyncio.sleep(0.1)
    return None, f"still running after {seconds:.0f} s"


async def demonstrate(runner: RecordingRunner, chosen, *, models: dict, registry: ModelRegistry, image: str,
                      project: str, sides: list, persistence, rng: random.Random, delay: float, ceilings: dict,
                      say=print):
    store = runner._workspace_store()
    checks: list = []

    def check(label: str, subject, why_absent: str, failing) -> None:
        """A check whose subject never happened fails; else the first failing clause, or None."""
        if not subject:
            checks.append((label, None, why_absent))
            return
        clause = failing()
        checks.append((label, clause is None, clause))

    for side, other in zip(sides, reversed(sides)):
        side.other = other

    def agent(name: str):
        return general_agent(model=models[name], image=image, registry=registry, files=FILES, delegate=True,
                             budget=BudgetPolicy(run_ceiling_usd=ceilings[name]), keep=name == "piece",
                             max_turns=60 if name == "runaway" else 12,
                             **({"delegate_approval": ApprovalPolicy.REQUIRE_APPROVAL} if name == "asker" else {}))

    async def start(side: Tenancy, name: str):
        await asyncio.sleep(rng.random() * delay)
        made_agent = agent(name)
        side.ceilings[name] = made_agent.budget.run_ceiling_usd
        handle = await runner.start(made_agent.spec, TASKS[name], made_agent.config(side.tenant, project))
        side.runs[name] = handle.run_id
        if name == "canceller":
            side.sleeper = await _sleeping_child(runner, persistence, handle.run_id)
            if side.sleeper is not None:
                side.sleeper_reached = await reach(runner, chosen, side.other.tenant, project, side.sleeper[1])
                handle.cancel()
        result = await handle.result()
        if name == "canceller" and side.sleeper is not None:
            side.sleeper_end = await _ended(runner, persistence, side.sleeper[0], 60)
        return name, result

    operators = [asyncio.ensure_future(operator(runner, s.tenant, project, s.asked)) for s in sides]
    try:
        starts = [(side, name) for side in sides for name in NAMES]
        rng.shuffle(starts)
        ended = await asyncio.wait_for(asyncio.gather(*(start(side, name) for side, name in starts)), 1200)
    finally:
        for task in operators:
            task.cancel()
        await asyncio.gather(*operators, return_exceptions=True)
    for (side, _), (name, result) in zip(starts, ended):
        side.results[name] = result
        say(f"{side.tenant} {name} {result.run_id}: {result.status.value} {result.error or ''} spent {result.cost_usd}")

    kids = {(s.tenant, name): await children_of(runner, persistence, s.runs.get(name)) for s in sides for name in NAMES}

    # --- 1. reservations and ceilings -------------------------------------------------------------
    spent: dict = {}
    together: dict = {}
    for side in sides:
        for name in NAMES:
            run_id = side.runs[name]
            parent_costs = costs(await events_of(runner, persistence, run_id))
            children = []
            for kid in kids[(side.tenant, name)]:
                said = await events_of(runner, persistence, kid)
                names, held = await reservation_of(runner, persistence, kid)
                children.append((kid, names, held, costs(said), ending(said)))
            # Pre-review F3: a parent waits in its delegate_task call while its child runs, so the agents
            # of one run that run at once are the most children one of its turns started, or itself.
            turns = await history_of(runner, side.tenant, project, run_id)
            together[(side.tenant, name)] = max([sum(1 for c in m.tool_calls if c.name == DELEGATE_TOOL)
                                                 for m in turns] + [1])
            spent[(side.tenant, name)] = (side.results[name], parent_costs, children)

    def total(key) -> Decimal:
        """A run's spend against its ceiling: its own calls and its children's (FR-184). RunResult's
        cost_usd is the run's own calls alone."""
        _, parent_costs, children = spent[key]
        return sum(parent_costs, Decimal(0)) + sum((c for _, _, _, cs, _ in children for c in cs), Decimal(0))

    def bounded() -> str | None:
        for (tenant, name), (result, parent_costs, children) in spent.items():
            every = parent_costs + [c for _, _, _, cs, _ in children for c in cs]
            largest = max(every) if every else Decimal(0)
            ceiling = next(s for s in sides if s.tenant == tenant).ceilings[name]
            bound = slack(ceiling, largest, together[(tenant, name)], runner._limits.max_concurrent_subagents)
            if result.cost_usd is None or total((tenant, name)) > bound:
                return (f"{tenant} {name}: spent {total((tenant, name))} against {ceiling} with {len(children)} "
                        f"child(ren), {together[(tenant, name)]} agent(s) running at once")
            for kid, names, held, cs, _ in children:
                if len(names) != 1 or not names[0].startswith("delegated:") or held is None:
                    return f"{tenant} {name}'s child {kid} ran on {names}, not one delegated reservation of its own"
                if sum(cs) > held + (max(cs) if cs else 0):
                    return f"{tenant} {name}'s child {kid} spent {sum(cs)} against its reservation of {held}"
        return None

    def stopped() -> str | None:
        for side in sides:
            result, _, children = spent[(side.tenant, "runaway")]
            _, _, held, cs, (status, reason) = children[0]
            if reason != BUDGET_EXCEEDED:
                return f"{side.tenant}: the runaway's child ended {status} ({reason}) after {len(cs)} call(s)"
            if not sum(cs) >= held:
                return f"{side.tenant}: the runaway's child stopped at {sum(cs)}, short of its reservation of {held}"
            if result.error != BUDGET_EXCEEDED:
                return f"{side.tenant}: the runaway ended {result.status.value} ({result.error})"
            if not total((side.tenant, "runaway")) >= side.ceilings["runaway"]:
                return (f"{side.tenant}: the runaway stopped at {total((side.tenant, 'runaway'))}, short of "
                        f"{side.ceilings['runaway']}")
        return None

    runaways = [spent[(s.tenant, "runaway")][2] for s in sides]
    check("each child stayed within its reservation and each parent within its ceiling, each within one call's "
          "cost per running agent; each runaway's child was stopped by its reservation and the runaway by its ceiling",
          all(len(found) == 1 and found[0][3] for found in runaways),
          f"a runaway's child made no priced call: {[len(f) for f in runaways]} child(ren)",
          lambda: bounded() or stopped())

    # --- 2. files -------------------------------------------------------------------------------
    pieces = {}
    for side in sides:
        run_id = side.runs["piece"]
        calls = await calls_of(runner, side.tenant, project, run_id)
        events = called(await events_of(runner, persistence, run_id))
        delegated = [(c, r) for c, r in calls if c.name == DELEGATE_TOOL]
        good = [(c, r) for c, r in delegated if c.arguments.get("files") == [NAMED]]
        hostile = {path: [(c, events.get(c.id)) for c, _ in delegated if c.arguments.get("files") == [path]]
                   for path in HOSTILE}
        child = kids[(side.tenant, "piece")][0] if kids[(side.tenant, "piece")] else None
        rows = await asyncio.to_thread(store.workspaces, side.tenant, project, child) if child else []
        lists = [(c.arguments.get("path"), r) for c, r in (await calls_of(runner, side.tenant, project, child)
                                                           if child else []) if c.name == "list_workspace"]
        commands = await asyncio.to_thread(store.commands, side.tenant, project)
        own = [c for c in commands if c.run_id == run_id]
        pieces[side.tenant] = {
            "good": good, "hostile": hostile, "child": child, "kids": kids[(side.tenant, "piece")],
            "files": [r.limits.get("files") for r in rows],
            # Each folder the child listed, by the names in it; None for a listing that failed.
            "listed": {path: None if r is None or r.is_error else
                       sorted(line.split("\t")[0] for line in r.content.splitlines() if "\t" in line)
                       for path, r in lists},
            "read": [c for c in commands if child and c.run_id == child and list(c.argv) == ["cat", NAMED]
                     and c.exit_code == 0],
            "made": {path: [c for c in own if c.exit_code == 0 and (list(c.argv)[:1] == [program])]
                     for path, program in ((LINK, "ln"), (BIG, "dd"))}}

    def piece_clause(tenant: str, found: dict) -> str | None:
        if found["files"] != [{NAMED: hashlib.sha256(FILES[NAMED]).hexdigest()}]:
            return f"{tenant}: the child's workspace recorded the files {found['files']}"
        # Live, a listing the child mangles fails and says nothing (M54 live run 6): the two it was
        # given must show the named file alone.
        if [found["listed"].get(path) for path in (".", "data")] != [["data"], ["a.txt"]]:
            return f"{tenant}: the child's workspace held {found['listed']}, not {NAMED} alone"
        if not found["read"]:
            return f"{tenant}: the child never read {NAMED} with a command"
        report = json.loads(found["good"][0][1].content) if found["good"][0][1] is not None else {}
        if report.get("run_id") != found["child"] or report.get("status") != RunStatus.COMPLETED.value:
            return f"{tenant}: the parent's tool result was {report}, not its completed child's"
        for path, attempts in found["hostile"].items():
            expected = "ToolValidationError" if path == OUTSIDE else TOOL_RAISED
            if not all(e is not None and e.get("is_error") is True and e.get("error_type") == expected
                       for _, e in attempts):
                return f"{tenant}: {HOSTILE[path]} was not refused as {expected}: {[e for _, e in attempts]}"
        # Live, a model may repeat the good call (M54 live run 3): each good call's child is its own, and
        # the property is that the hostile calls started none.
        asked = sorted(json.loads(r.content).get("run_id") for _, r in found["good"] if r is not None and not r.is_error)
        if sorted(found["kids"]) != asked:
            return (f"{tenant}: the piece started {len(found['kids'])} children, not the {len(asked)} its good "
                    f"call(s) asked for")
        return None

    check("a child's workspace held only the files its call named; a path outside the parent's workspace, a link "
          "and a file past the transfer's bound were each refused, spawning nothing",
          all(f["good"] and f["child"] and all(f["hostile"].values()) and all(f["made"].values())
              for f in pieces.values()),
          f"a piece did not make every call: "
          f"{ {t: (len(f['good']), {p: len(a) for p, a in f['hostile'].items()}, {p: len(m) for p, m in f['made'].items()}) for t, f in pieces.items()} }",
          lambda: next((why for tenant, found in pieces.items() if (why := piece_clause(tenant, found))), None))

    # --- 3. depth ----------------------------------------------------------------------------------
    attempts, deeper = {}, {}
    for side in sides:
        nested = kids[(side.tenant, "nester")]
        attempts[side.tenant] = [(c, r) for kid in nested for c, r in await calls_of(runner, side.tenant, project, kid)
                                 if c.name == DELEGATE_TOOL]
        every_child = [kid for name in NAMES for kid in kids[(side.tenant, name)]]
        deeper[side.tenant] = [g for kid in every_child for g in await children_of(runner, persistence, kid)]
    check("a child's delegate_task was refused as an unknown tool, and no run reached depth 2",
          all(attempts.values()), "a nester's child never tried to delegate",
          # DECISION-94bfee7c: keyed on the result's provenance source, never on its text.
          lambda: next((f"{tenant}: a child's delegate_task was not refused as unknown: {found}"
                        for tenant, found in attempts.items()
                        if not all(r is not None and r.is_error and r.provenance.source_uri_or_hash == NOT_FOUND
                                   for _, r in found)), None)
          or next((f"{tenant}: runs reached depth 2: {found}" for tenant, found in deeper.items() if found), None))

    # --- 4. approval -------------------------------------------------------------------------------
    approvals = {}
    for side in sides:
        run_id = side.runs["asker"]
        views = await runner.interruptions(side.tenant, project, run_id=run_id, pending_only=False)
        spawns = [v for v in views if v.interruption.tool_name == DELEGATE_TOOL]
        results = [r for c, r in await calls_of(runner, side.tenant, project, run_id) if c.name == DELEGATE_TOOL]
        approvals[side.tenant] = {
            "spawns": spawns, "asked": [(objective, count, approve) for r, tool, objective, count, approve in side.asked
                                        if r == run_id and tool == DELEGATE_TOOL],
            "kids": kids[(side.tenant, "asker")], "errors": [r is not None and r.is_error for r in results],
            "calls": len(results), "status": side.results["asker"].status}

    def approval_clause(tenant: str, found: dict) -> str | None:
        principal = operator_of(tenant)
        if found["status"] is not RunStatus.COMPLETED:
            return f"{tenant}: the asker ended {found['status'].value}"
        if len(found["spawns"]) != 2 or not all(v.decision is not None and v.decision.principal == principal
                                                 for v in found["spawns"]):
            return f"{tenant}: {len(found['spawns'])} spawn(s) asked, decided {[v.decision for v in found['spawns']]}"
        if found["asked"] != [(HELLO, 0, True), (GOODBYE, 1, False)]:
            return f"{tenant}: a child existed before its spawn's decision: {found['asked']}"
        if len(found["kids"]) != 1:
            return f"{tenant}: {len(found['kids'])} children, not the one approved"
        if found["errors"] != [False, True]:
            return f"{tenant}: the spawns' results were errors {found['errors']}, not the refused one alone"
        return None

    # Its subject: each asker called delegate_task twice, as its history holds; each call then waits for a
    # decision, so each has its interruption (the property).
    check("every spawn under REQUIRE_APPROVAL waited for its decision, and the refused one started nothing",
          all(f["calls"] == 2 for f in approvals.values()),
          f"an asker did not ask for two spawns: {[f['calls'] for f in approvals.values()]}",
          lambda: next((why for tenant, found in approvals.items() if (why := approval_clause(tenant, found))), None))

    # --- 5. tenancy ----------------------------------------------------------------------------------
    # Workspaces: the piece's kept workspace, and the canceller's child's while it ran (above).
    kept = {s.tenant: (made(await events_of(runner, persistence, s.runs["piece"])) or [None])[0] for s in sides}
    reached = {}
    for side in sides:
        target = kept[side.other.tenant]
        if target is not None:
            reached[side.tenant] = {"kept": await reach(runner, chosen, side.tenant, project, target)}
        if side.other.sleeper_reached is not None:
            reached.setdefault(side.tenant, {})["child's"] = side.other.sleeper_reached
    # Round 1 F-B, the positive control: the same calls on a tenancy's own kept workspace succeed, so a
    # release and a snapshot refused to everybody fail. Each tenancy snapshots its own and releases the
    # snapshot; the first also releases its workspace, its listing kept first as the subject of nothing
    # remaining after aclose(), and the second's is left for aclose() to remove.
    own = {}
    for side in sides:
        target = kept[side.tenant]
        if target is None:
            continue
        try:
            ref = await runner.snapshot_workspace(side.tenant, project, target)
            snapped = await runner.release_snapshot(side.tenant, project, ref.snapshot_id)
        except Exception as exc:  # noqa: BLE001 - any refusal is the failure, by class
            snapped = type(exc).__name__
        released = None
        if side is sides[0]:
            side.held = await remains(chosen, side.tenant)
            released = await runner.release_workspace(side.tenant, project, target)
        own[side.tenant] = (snapped, released, bool(await chosen.list_owned({"agentsdk.workspace": target})))
    expected = {sides[0].tenant: (True, True, False), sides[1].tenant: (True, None, True)}
    check("neither tenancy could release or snapshot the other's workspaces: a parent's kept one, or a child's "
          # Both kept workspaces there, both tenancies attempted their own: `own` needs no clause of its own.
          "while it ran; each could its own", len(reached) == 2 and all(len(r) == 2 for r in reached.values()),
          f"a tenancy had no workspace of the other's to attempt: {reached}",
          lambda: next((f"{tenant}: the other tenancy's {which} workspace: released {r}, snapshot {s}, still there "
                        f"{still}" for tenant, found in reached.items() for which, (r, s, still) in found.items()
                        if (r, s, still) != (False, "unavailable", True)), None)
          or next((f"{tenant}: its own kept workspace: snapshot taken and released {s}, released {r}, still there "
                   f"{still}" for tenant, (s, r, still) in own.items() if (s, r, still) != expected[tenant]), None))

    # Rows: what each tenancy reads of its own holds its runs (the witness), and nothing of the other's.
    own_kids = {s.tenant: {k for name in NAMES for k in kids[(s.tenant, name)]} for s in sides}
    own_ids = {s.tenant: set(s.runs.values()) | own_kids[s.tenant] for s in sides}
    readings = {}
    for side in sides:
        tenant = side.tenant
        readings[tenant] = {
            "workspaces": {w.run_id for w in await asyncio.to_thread(store.workspaces, tenant, project)},
            "commands": {c.run_id for c in await asyncio.to_thread(store.commands, tenant, project)},
            "interruptions": {v.interruption.run_id for v in await runner.interruptions(tenant, project,
                                                                                           pending_only=False)},
        }
    check("neither tenancy read the other's rows: workspaces, commands and interruptions, a parent's or a child's",
          # Pre-review F7: a parent's rows and, where children have them (no child asks for approval), a
          # child's.
          all(readings[s.tenant][kind] & set(s.runs.values()) for s in sides for kind in readings[s.tenant])
          and all(readings[s.tenant][kind] & own_kids[s.tenant] for s in sides for kind in ("workspaces", "commands")),
          f"a tenancy read none of its own parents' rows, or none of its children's: {readings}",
          lambda: next((f"{s.tenant} read {kind} of {s.other.tenant}'s runs" for s in sides for kind, ids in
                        readings[s.tenant].items() if ids & own_ids[s.other.tenant]), None)
          or next((f"{s.tenant}'s {kind} were not its own" for s in sides for kind, ids in readings[s.tenant].items()
                   if not ids <= own_ids[s.tenant]), None))

    # Answers: a parent's and its child's history, read by its owner (the witness) and by the other.
    answers = {}
    for side in sides:
        targets = [side.other.runs["piece"], *kids[(side.other.tenant, "piece")][:1]]
        answers[side.tenant] = [(run_id, len(await history_of(runner, side.other.tenant, project, run_id)),
                                 len(await history_of(runner, side.tenant, project, run_id))) for run_id in targets]
    check("neither tenancy read the other's answers: a parent's or a child's history",
          all(len(found) == 2 and all(own for _, own, _ in found) for found in answers.values()),
          f"a tenancy's own parent or child had no history to read: {answers}",
          lambda: next((f"{tenant} read {theirs} message(s) of the other tenancy's run {run_id}"
                        for tenant, found in answers.items() for run_id, _, theirs in found if theirs), None))

    # --- 6. cancellation ----------------------------------------------------------------------------
    cancelled = {}
    for side in sides:
        if side.sleeper is None:
            continue
        kid, workspace_id = side.sleeper
        rows = await asyncio.to_thread(store.workspaces, side.tenant, project, kid)
        # Pre-review F1 and round 1 F-A: the long command's own end. Every cancelled command is marked
        # killed "cancelled", so a kill is one with no kill error that ended inside the kill's bound: an
        # abandoned command runs at least _KILL_SECONDS past its start, one run out SLEEP_SECONDS.
        commands = [(p.get("killed"), p.get("kill_error"), (p.get("duration_ms") or 0) < KILL_MS)
                    for kind, p in await events_of(runner, persistence, kid)
                    if kind == EventType.WORKSPACE_COMMAND_ENDED.value]
        cancelled[side.tenant] = {"parent": side.results["canceller"].status, "child": side.sleeper_end,
                                  "commands": commands,
                                  "removed": [r.removed_at is not None for r in rows if r.workspace_id == workspace_id],
                                  "listed": bool(await chosen.list_owned({"agentsdk.workspace": workspace_id}))}
    check("cancelling a parent ended its running child cancelled, its long command killed by the cancellation, and the child's workspace was removed",
          len(cancelled) == 2, "a canceller's child never started its long command",
          lambda: next((f"{tenant}: the parent ended {found['parent'].value}" for tenant, found in cancelled.items()
                        if found["parent"] is not RunStatus.CANCELLED), None)
          or next((f"{tenant}: the child ended {found['child']}" for tenant, found in cancelled.items()
                   if (found["child"] or (None,))[0] != RunStatus.CANCELLED.value), None)
          or next((f"{tenant}: the child's long command ended (killed, kill error, inside {KILL_MS} ms) "
                   f"{found['commands']}, not killed by the cancellation"
                   for tenant, found in cancelled.items() if found["commands"] != [("cancelled", None, True)]), None)
          or next((f"{tenant}: the child's workspace rows say removed {found['removed']}, listed {found['listed']}"
                   for tenant, found in cancelled.items() if found["removed"] != [True] or found["listed"]), None))
    return checks


async def closed(chosen, runner, sides, checks: list) -> None:
    """However the demonstration ended, its Runner is closed, and then nothing of either tenancy may
    remain. Its subject: each tenancy's listing found its workspace (the piece's kept one) while it was
    there: before aclose(), or before the first tenancy released its own (round 1 F-B). The second's is
    still there for aclose() to remove."""
    before = {side.tenant: side.held if side.held is not None else await remains(chosen, side.tenant)
              for side in sides}
    if runner is not None:
        await runner.aclose()
    after = {side.tenant: await remains(chosen, side.tenant) for side in sides}
    label = "after aclose() nothing of either tenancy remains: workspace, network, volume or snapshot"
    if not all(before.values()):
        checks.append((label, None, f"a tenancy held nothing before aclose(): {before}"))
    else:
        left = {t: found for t, found in after.items() if found is None or found}
        checks.append((label, not left, f"remained: {left}" if left else None))


def report(checks) -> list[str]:
    """A check whose subject never happened fails in every mode (DECISION-21e7f6fb)."""
    failed = []
    for label, passed, why in checks:
        if passed is None:
            print(f"[FAIL] not exercised: {label} ({why})")
        else:
            print(f"[{'PASS' if passed else 'FAIL'}] {label}" + ("" if passed else f" -- {why}"))
        if not passed:
            failed.append(label)
    return failed


def docker_ready() -> str | None:
    """Why this example cannot use Docker here, or None."""
    try:
        if subprocess.run(["docker", "version"], capture_output=True, timeout=30).returncode != 0:
            return "docker not running"
        if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30).returncode != 0:
            return "the example image is not on this host (see the docstring to pull it)"
    except (OSError, subprocess.TimeoutExpired):
        return "docker not running"
    return None


async def _run(clients: dict, chosen, persistence, registry: ModelRegistry, *, models: dict, image: str,
               project: str, tenants: tuple[str, str], rng: random.Random, delay: float, ceilings: dict, say):
    runner = RecordingRunner(clients, model_registry=registry, workspace_provider=chosen, persistence=persistence,
                             max_concurrent_workspaces=32)
    checks, sides = [], [Tenancy(t) for t in tenants]
    try:
        checks += await demonstrate(runner, chosen, models=models, registry=registry, image=image, project=project,
                                    sides=sides, persistence=persistence, rng=rng, delay=delay, ceilings=ceilings,
                                    say=say)
    finally:
        await closed(chosen, runner, sides, checks)
    return checks, sides


# Scripted calls cost USD 0.10 each. The runaway's first call leaves 0.90, of which its child reserves a
# quarter, 0.225: the child's third call crosses it. The runaway then works on until its 1.00 stops it.
SCRIPTED_CEILINGS = {"piece": Decimal("5.00"), "runaway": Decimal("1.00"), "nester": Decimal("5.00"),
                     "asker": Decimal("5.00"), "canceller": Decimal("5.00")}
SCRIPTED_MODELS = {name: "m:scripted" for name in NAMES} | {"nester": "s:scripted"}


async def offline(store: str = "memory", provider_name: str = "fake", *, seed: int | None = None,
                  tenant: str = TENANT, project: str | None = None, quiet: bool = False):
    """The exit signal with the scripted model; the tests call it, ten times on Docker. A project of its
    own each time, so one repetition's records never meet another's."""
    rng = random.Random(seed)
    persistence = None
    if store == "postgres":
        from dotenv import load_dotenv

        from agentsdk import Persistence
        from agentsdk.config import normalise_database_url

        load_dotenv()
        persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]), create_schema=False)
    chosen = provider(provider_name)
    image = IMAGE if provider_name == "docker" else FAKE_IMAGE
    delay = 0.5 if provider_name == "docker" else 0.01
    scripted = ScriptedModel(rng, delay)
    return await _run({"m": scripted, "s": scripted}, chosen, persistence, scripted_registry(),
                      models=SCRIPTED_MODELS, image=image, project=project or f"contention45-{uuid.uuid4().hex[:8]}",
                      tenants=(tenant, tenant + ELSEWHERE), rng=rng, delay=delay, ceilings=SCRIPTED_CEILINGS,
                      say=(lambda *a: None) if quiet else print)


# DECISION-61bead9f: live on openai.gpt-4o-mini, three runs within USD 1. A live model may stop a
# runaway's work itself (M51; M54's first live runs answered at 0.0018 of a 0.0015 ceiling), so the
# runaway's ceiling sits between its first call and that call with its child's: measured in M54's live
# runs, a first call costs USD 0.00043 to 0.00052 and the child's one call 0.00029 to 0.00039. The first
# leaves a quarter of the rest to the child, below one call's cost, so the child's call crosses its
# reservation and takes the runaway past its ceiling: its next call is refused. Pre-review F5: the
# first call alone must not reach it, or no child is started; and every ceiling spent in full, three
# runs stay within USD 1 (0.3212 each).
LIVE_MODEL = "openai.gpt-4o-mini"
LIVE_CEILINGS = {"piece": Decimal("0.04"), "runaway": Decimal("0.0006"), "nester": Decimal("0.04"),
                 "asker": Decimal("0.04"), "canceller": Decimal("0.04")}


def live():
    from dotenv import load_dotenv

    from agentsdk import Persistence
    from agentsdk.config import Settings, normalise_database_url
    from agentsdk.postgres import close_pools
    from agentsdk.providers import OpenAICompatibleModelClient

    load_dotenv()
    persistence = Persistence.postgres(normalise_database_url(os.environ["DATABASE_URL"]))
    settings = Settings.from_env(load_dotfile=False)

    async def go():
        client = OpenAICompatibleModelClient(base_url=settings.base_url, api_key=settings.api_key, model=LIVE_MODEL)
        try:
            rng = random.Random()
            # Pre-review F5: tenancies of this run's own (DECISION-61bead9f), so nothing another run
            # left under a shared name can pass or fail its checks.
            tenant = f"{TENANT}-{uuid.uuid4().hex[:8]}"
            checks, sides = await _run(
                {"m": client, "s": ScriptedModel(rng, 0.0)}, DockerProvider(), persistence, live_registry(),
                models={name: f"m:{LIVE_MODEL}" for name in NAMES} | {"nester": "s:scripted"}, image=IMAGE,
                project=f"contention45-{uuid.uuid4().hex[:8]}", tenants=(tenant, tenant + ELSEWHERE), rng=rng,
                delay=1.0, ceilings=LIVE_CEILINGS, say=print)
            ids = [run_id for side in sides for run_id in side.runs.values()]
            print(f"\nrun ids (to remove the demo's rows, children included by parent): {' '.join(ids)}")
            return checks
        finally:
            await client.aclose()

    try:
        return asyncio.run(go())
    finally:
        close_pools()


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="in memory, scripted model, FakeProvider")
    mode.add_argument("--scripted", action="store_true", help="in memory, scripted model, Docker")
    args = parser.parse_args()
    if not args.offline and (why := docker_ready()) is not None:
        print(f"[SKIP] {why}")
        raise SystemExit(0)
    if args.offline or args.scripted:
        checks, _ = asyncio.run(offline(provider_name="fake" if args.offline else "docker"))
    else:
        checks = live()
    failed = report(checks)
    if failed:
        raise SystemExit(f"failed: {failed}")
