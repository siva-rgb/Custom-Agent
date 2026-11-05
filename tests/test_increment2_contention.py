"""M47 gate: increment 2's exit signal (FR-171, AC-105, NFR-46 to NFR-49).

Example 41 is the exit signal: one plan whose children try every way out past an allow list, use a
hostile sandboxed MCP server, have a workspace holding links snapshotted and restored, and burn CPU
until their compute ceiling stops them. The gates hold its plan on `FakeProvider`, on both stores,
with scripted children and with children that quote earlier results into later calls
(DECISION-a00773b0); a child that does nothing fails every check, never passes one
(DECISION-a7d6a1f5). Each check fails without its subject (AC-105): on both providers for the
snapshot's tenancy check and the compute charge, on a real daemon for the internal network, the
proxy's address and SNI checks and the server's container removal (DECISION-33856cec). The
rebinding name is rotated on the fake, a public one on Docker (DECISION-18bd3f65).

Tests marked `docker` run live before review with `pytest -m docker`: the plan ten times on each
store with shuffled starts and random delays, and the removals. Every row and Docker object a test
writes is removed by its own tenancies.
"""

from __future__ import annotations

import asyncio
import json
import importlib.util
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

from agentsdk import api
from agentsdk.primitives import Role, ToolCall
from agentsdk.config import normalise_database_url
from agentsdk.workspace import DockerProvider, FakeProvider, WorkspaceSpec, _Sink
from agentsdk.workspace import egress as eg
from agentsdk.workspace import manager as mgr

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
PREFIX = "SYN-m47-"
STORES = ["memory", "postgres"]
TENANTS: list[str] = []
NL = chr(10)


def _example(name: str):
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py").replace("-", "_"), REPO / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EX41 = _example("41_increment2_contention.py")


def _docker_leftovers(tenant: str) -> None:
    """Removes what a mutant left on the daemon for this test's tenancies: never anything else."""
    for owner in (tenant, tenant + EX41.ELSEWHERE):
        for kind, listing in (("container", ["ps", "-aq"]), ("volume", ["volume", "ls", "-q"]),
                              ("network", ["network", "ls", "-q"])):
            found = subprocess.run(["docker", *listing, "--filter", f"label=agentsdk.tenant={owner}"],
                                   capture_output=True, text=True, timeout=60).stdout.split()
            if found:
                subprocess.run(["docker", kind, "rm", "-f", *found], capture_output=True, timeout=120)


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    TENANTS.clear()
    yield
    if TENANTS and subprocess.run(["docker", "version"], capture_output=True).returncode == 0:
        for tenant in TENANTS:
            _docker_leftovers(tenant)
    if DSN and TENANTS:
        owners = [*TENANTS, *(t + EX41.ELSEWHERE for t in TENANTS)]
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = ANY(%s)", (owners,))]
            for table in ("workspace_snapshots", "workspace_connections", "workspace_commands", "workspaces",
                          "mcp_tool_acceptances", "mcp_tool_versions"):
                conn.execute(f"DELETE FROM {table} WHERE tenant_id = ANY(%s)", (owners,))
            conn.execute("DELETE FROM interruption_decisions WHERE interruption_id IN (SELECT interruption_id"
                         " FROM interruptions WHERE run_id = ANY(%s))", (ids,))
            conn.execute("DELETE FROM interruptions WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM standing_decisions WHERE run_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM artifacts WHERE source_run = ANY(%s)", (ids,))
            for table in ("plan_node_states", "plan_versions", "run_events", "messages", "execution_manifests"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
            while conn.execute("DELETE FROM runs r WHERE run_id = ANY(%s) AND NOT EXISTS "
                               "(SELECT 1 FROM runs c WHERE c.parent_run_id = r.run_id)", (ids,)).rowcount:
                pass


def tenancy() -> str:
    tenant = f"{PREFIX}{uuid.uuid4().hex[:10]}"
    TENANTS.append(tenant)
    return tenant


async def example_41(store: str, provider_name: str = "fake", **options):
    """Example 41's checks as {label: (passed, why)}."""
    checks, _ = await EX41.offline(store, provider_name, tenant=tenancy(), quiet=True, **options)
    return {label: (passed, why) for label, passed, why in checks}


def failed(checks: dict) -> dict:
    return {label: why for label, (passed, why) in checks.items() if passed is not True}


ALLOWED = "the allowed pair was fetched through the proxy, recorded allowed with its address, by the command that made it"
REFUSED = ("an IP literal, an unlisted host, a name resolving privately, another site by SNI, plain HTTP and a second "
           "request were each refused and recorded by reason")
REBOUND = ("a rebinding name was never reached at a private address: each attempt refused not_global or recorded "
           "at a global one")
DIRECT = ("a direct connection to the address the proxy reached, the host directly and a name lookup failed inside "
          "the workspace")
SERVED = "the sandboxed server served the plan's child: its echo came back"
RESTORED = "restored in its own tenancy within bounds, it reads the state at the snapshot, the link still a link"
CONTAINED = ("the sandboxed server reached nothing: the network, the host's database port, a neighbour and a name "
             "lookup, each against a control")
KILLED = "the server was killed with its Runner: listed before aclose(), nothing of it after, its row removed aclose"
TENANCY = "another tenancy's workspace could not start from the snapshot"
CHARGED = "every command of the burning child was charged at least its CPU reading"
STOPPED = "the burning child was stopped within one command's cost of its ceiling, no command started after"
NOTHING_LEFT = "after aclose() no workspace, network, proxy or volume of the example remains"
OFFLINE_CHECKS, DOCKER_CHECKS = 14, 18


# =================================================================================================
# FR-171 on FakeProvider, both stores
# =================================================================================================


@pytest.mark.parametrize("store", STORES)
async def test_example_41_holds_every_check_offline(store):
    checks = await example_41(store, seed=42)
    assert len(checks) == OFFLINE_CHECKS and not failed(checks), failed(checks)
    assert {ALLOWED, REFUSED, REBOUND, KILLED, TENANCY, CHARGED, STOPPED} <= set(checks)


@pytest.mark.parametrize("store", STORES)
async def test_example_41_holds_under_children_that_quote_earlier_results(store, monkeypatch):
    """DECISION-a00773b0: each child also writes the results it read, verbatim, into later calls,
    and every check still holds."""
    quoted: list[str] = []
    real = EX41.ScriptedChild.send

    async def send(self, request):
        response = await real(self, request)
        quoted.extend(c.arguments.get("content", c.arguments.get("text")) for c in response.message.tool_calls
                      if c.id.startswith("q"))
        return response

    monkeypatch.setattr(EX41.ScriptedChild, "send", send)
    checks = await example_41(store, seed=7, quote=True)
    assert len(checks) == OFFLINE_CHECKS and not failed(checks), failed(checks)
    # Its subject: results were quoted, the egress probe's own output among them.
    assert len(quoted) >= 4 and any("not_listed" in text or "allowed" in text for text in quoted), quoted


async def test_example_41_passes_nothing_when_no_child_does_anything(monkeypatch):
    """A subject that never happened fails its check (DECISION-a7d6a1f5). The one that passes has
    a subject of its own: the server's workspace, made at the first look before any child ran, is
    gone after aclose()."""
    async def declines(self, request):
        return EX41._reply(content="I will not run that.")

    monkeypatch.setattr(EX41.ScriptedChild, "send", declines)
    checks = await example_41("memory")
    assert [label for label, (passed, _) in checks.items() if passed is True] == [NOTHING_LEFT]
    assert sum(1 for passed, _ in checks.values() if passed is None) == OFFLINE_CHECKS - 1


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_example_41_holds_whatever_the_childrens_start_order(seed):
    checks = await example_41("memory", seed=seed)
    assert len(checks) == OFFLINE_CHECKS and not failed(checks), failed(checks)


@pytest.mark.parametrize("store", STORES)
async def test_example_41_holds_when_a_child_asks_for_both_its_commands_at_once(store, monkeypatch):
    """M47 live run: a model asked for the snapshot child's two commands in one response, whose
    approvals are all settled before either runs. The operator refuses the second, asked before the
    first had run; the child asks again alone, and the snapshot falls between the two."""
    real = EX41.ScriptedChild.send
    seen = {"batched": 0, "rejected": 0}

    async def batching(self, request):
        text = request.instructions or ""
        turns = json.loads(text[text.index("TURNS: ") + 7:].split(NL)[0]) if "TURNS: " in text else []
        if not turns or turns[0][0][1].get("argv") != EX41.ARGV["build"]:
            return await real(self, request)
        turn = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        results = {r.tool_call_id: r for m in request.messages for r in m.tool_results}
        if turn == 0:
            seen["batched"] += 1
            return EX41._reply([ToolCall(id="t0-0", name="run_command", arguments={"argv": EX41.ARGV["build"]}),
                                ToolCall(id="t0-1", name="run_command", arguments={"argv": EX41.ARGV["after"]})])
        if turn == 1 and results["t0-1"].is_error:
            seen["rejected"] += 1
            nonce = next(line.split()[1] for line in EX41.stdout_of(results["t0-0"].content).splitlines()
                         if line.startswith("nonce "))
            return EX41._reply([ToolCall(id="t1-0", name="run_command",
                                         arguments={"argv": [*EX41.ARGV["after"][:3], nonce]})])
        return EX41._reply(content="Done.")

    monkeypatch.setattr(EX41.ScriptedChild, "send", batching)
    checks = await example_41(store, seed=11)
    assert seen == {"batched": 1, "rejected": 1}, seen
    assert len(checks) == OFFLINE_CHECKS and not failed(checks), failed(checks)


def _probes_twice(monkeypatch, second_fetch_fails: bool) -> None:
    """The egress child runs its probe twice in one response, as a live model did (M47 live run);
    with `second_fetch_fails`, the second run's fetch through the proxy fails."""
    real_send, real_egress = EX41.ScriptedChild.send, EX41._egress
    ran = []

    async def send(self, request):
        response = await real_send(self, request)
        calls = list(response.message.tool_calls)
        probe = [c for c in calls if EX41.runs_probe(c.arguments.get("argv"), "egress")]
        if probe and probe[0].id == "t0-0":
            calls.append(ToolCall(id="t0-9", name="run_command", arguments=dict(probe[0].arguments)))
            return EX41._reply(calls)
        return response

    async def egress(cmd):
        ran.append(cmd)
        if second_fetch_fails and len(ran) == 2:
            async def unreachable(host, port):
                raise OSError("no fetch")

            cmd.connect = unreachable
        return await real_egress(cmd)

    monkeypatch.setattr(EX41.ScriptedChild, "send", send)
    monkeypatch.setattr(EX41, "_egress", egress)


async def test_example_41_judges_every_run_of_the_egress_probe(monkeypatch):
    _probes_twice(monkeypatch, second_fetch_fails=False)
    checks = await example_41("memory", seed=4)
    assert len(checks) == OFFLINE_CHECKS and not failed(checks), failed(checks)


async def test_a_second_run_of_the_egress_probe_that_failed_its_fetch_fails_the_check(monkeypatch):
    _probes_twice(monkeypatch, second_fetch_fails=True)
    checks = await example_41("memory", seed=4)
    passed, why = checks[ALLOWED]
    assert passed is False and why.startswith("run 2 of 2:"), checks[ALLOWED]


def test_example_41s_live_children_are_given_room_to_copy_their_probes():
    """M47 live run: a live egress child ran out of output copying its probe at the default limit."""
    live = EX41.roles("m:x", FakeProvider(), "93.184.215.14", "10.99.0.2", "93.184.215.14", live=True)
    assert {r.max_output_tokens for r in live.values()} == {EX41.LIVE_OUTPUT_TOKENS}
    scripted = EX41.roles("child:m", FakeProvider(), "93.184.215.14", "10.99.0.2", "93.184.215.14")
    assert {r.max_output_tokens for r in scripted.values()} == {None}


async def test_the_restore_check_fails_when_the_snapshot_is_the_live_tree(monkeypatch):
    """M47 pre-review F1: a snapshot that aliased the live tree passed, since nothing changed after
    it; the second command now changes the workspace, and the restore must not see it."""
    from agentsdk.workspace import SnapshotHandle

    async def aliased(self, handle, labels):
        files = self._space(handle).files
        taken = SnapshotHandle(labels["agentsdk.snapshot"], dict(labels), files.size())
        self._snapshots[taken.snapshot_id] = (taken, files)
        return taken

    monkeypatch.setattr(FakeProvider, "snapshot", aliased)
    checks = await example_41("memory", seed=3)
    passed, why = checks[RESTORED]
    assert passed is False and "changed" in why, checks[RESTORED]


async def test_the_restore_check_needs_the_workspace_changed_after_its_snapshot(monkeypatch):
    """Its subject: the second command, started after the snapshot was recorded, read back the a.txt
    it rewrote. One that changed nothing leaves the check unexercised, never passed."""
    real = EX41.python

    async def unchanged(cmd):
        if EX41.runs_probe(cmd.argv, "after"):
            cmd.out(f"after {cmd.argv[3]} {cmd.files.read('a.txt').decode()}\n")
            return 0
        return await real(cmd)

    monkeypatch.setattr(EX41, "python", unchanged)
    checks = await example_41("memory", seed=3)
    assert checks[RESTORED][0] is None, checks[RESTORED]


def _captured(monkeypatch) -> list:
    made = []
    real = EX41.provider

    def provider(name):
        made.append(real(name))
        return made[-1]

    monkeypatch.setattr(EX41, "provider", provider)
    return made


async def test_nothing_remains_when_the_example_raises_after_its_plan(monkeypatch):
    """M47 pre-review F2: aclose() was called inline, so a failure after the plan left workspaces."""
    made = _captured(monkeypatch)

    async def raises(chosen, neighbour):
        raise RuntimeError("a failure after the plan")

    monkeypatch.setattr(EX41, "controls", raises)
    with pytest.raises(RuntimeError):
        await example_41("memory", seed=3)
    assert made and not made[0]._spaces and not made[0]._snapshots, (made[0]._spaces, made[0]._snapshots)


async def test_nothing_remains_when_the_example_is_cancelled_mid_plan(monkeypatch):
    made = _captured(monkeypatch)
    started = asyncio.Event()
    real = EX41.ScriptedChild.send

    async def send(self, request):
        started.set()
        return await real(self, request)

    monkeypatch.setattr(EX41.ScriptedChild, "send", send)
    task = asyncio.ensure_future(example_41("memory", seed=3))
    await asyncio.wait_for(started.wait(), 30)
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert made and not made[0]._spaces, made[0]._spaces


async def test_a_server_call_is_judged_whatever_its_id(monkeypatch):
    """M47 pre-review F3: calls whose id began with q were set aside as a scripted child's quotes;
    a live model's ids are its own."""
    real = EX41.ScriptedChild.send

    async def q_ids(self, request):
        response = await real(self, request)
        calls = [ToolCall(id="q" + c.id, name=c.name, arguments=c.arguments) if c.name.startswith(EX41.ALIAS) else c
                 for c in response.message.tool_calls]
        return EX41._reply(calls) if calls else response

    monkeypatch.setattr(EX41.ScriptedChild, "send", q_ids)
    checks = await example_41("memory", seed=3)
    assert checks[SERVED][0] is True, checks[SERVED]


def test_example_41_runs_offline_as_a_program():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "41_increment2_contention.py"), "--offline"],
                          capture_output=True, text=True, timeout=900, cwd=REPO)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
    assert done.stdout.count("[PASS]") == OFFLINE_CHECKS and "FAIL" not in done.stdout


def test_a_probe_is_recognised_by_its_program_whatever_its_spacing_and_nothing_else():
    for name, argv in EX41.ARGV.items():
        assert EX41.runs_probe(argv, name), name
        assert EX41.runs_probe([*argv[:2], argv[2].replace(", ", ","), *argv[3:]], name), name
    build = EX41.ARGV["build"]
    assert not EX41.runs_probe([*build[:2], build[2] + "print(1)" + NL], "build")
    assert not EX41.runs_probe(["python3", *build[1:]], "build")
    assert not EX41.runs_probe([*build[:2], "("], "build")
    assert not EX41.runs_probe(build, "after")
    assert not EX41.runs_probe([*build[:2], "-" * 60000 + "1"], "build")


# =================================================================================================
# The rebinding name (DECISION-18bd3f65)
# =================================================================================================


async def test_the_fakes_resolver_asks_a_callable_at_each_resolution():
    answers = iter([["127.0.0.1"], ["93.184.215.14"], ["127.0.0.1"]])
    prov = FakeProvider(resolve={"rebinds.example": lambda: next(answers), "example.com": ["93.184.215.14"]},
                        sites={("example.com", 443): b"site"})
    handle = await prov.create(WorkspaceSpec(image=EX41.IMAGE, egress=("rebinds.example:443", "example.com:443")),
                               {"agentsdk.workspace": str(uuid.uuid4()), "agentsdk.tenant": tenancy()})
    proxy = prov._proxy(prov._spaces[handle.workspace_id])
    try:
        assert [await proxy.resolve("rebinds.example", 443) for _ in range(3)] == [
            ["127.0.0.1"], ["93.184.215.14"], ["127.0.0.1"]]
        assert await proxy.resolve("example.com", 443) == ["93.184.215.14"]
        reader, _ = await proxy.connect("93.184.215.14", 443)
        assert await reader.read() == b"site", "a fixed answer no longer finds its site"
    finally:
        await prov.remove(handle)


async def test_the_rebinding_check_fails_when_the_proxy_reaches_the_private_answer(monkeypatch):
    """KNOWLEDGE-1afcf6b2: the SDK breaking the property the check names fails it -- here the address
    check taken away on the fake, so the loopback answer is dialled."""
    monkeypatch.setattr(eg, "_global", lambda address: True)
    checks = await example_41("memory", seed=3)
    assert checks[REBOUND][0] is False, checks[REBOUND]
    assert checks[REFUSED][0] is False, checks[REFUSED]


async def test_the_rebinding_check_fails_when_its_control_never_sees_the_name_rebind(monkeypatch):
    """Its witness, independent of the proxy: the name answered both a private and a global address."""
    real = EX41.controls

    async def only_global(chosen, neighbour):
        return {**await real(chosen, neighbour), "rebinds": {"93.184.215.14"}}

    monkeypatch.setattr(EX41, "controls", only_global)
    checks = await example_41("memory", seed=3)
    assert checks[REBOUND][0] is False and "never rebound" in checks[REBOUND][1], checks[REBOUND]


async def test_the_rebinding_check_fails_when_the_proxy_is_never_handed_both_answers(monkeypatch):
    """The attack needs a check of the public answer, then a dial: a proxy only ever handed one
    answer was never attacked, whatever the control saw."""
    real = EX41.controls

    async def both(chosen, neighbour):
        return {**await real(chosen, neighbour), "rebinds": {"127.0.0.1", "93.184.215.14"}}

    monkeypatch.setattr(EX41, "controls", both)
    monkeypatch.setattr(EX41, "REBIND_ANSWERS", (["93.184.215.14"],))
    checks = await example_41("memory", seed=3)
    assert checks[REBOUND][0] is False and "both answers" in checks[REBOUND][1], checks[REBOUND]


async def test_the_rebinding_check_fails_when_an_allowed_attempt_reached_nothing_that_answers(monkeypatch):
    """FR-161: a proxy that dialled anywhere but the address it checked -- a fresh resolution's
    loopback, where nothing answers -- is recorded at the checked address with nothing back."""
    real = eg.Proxy.__init__

    def init(self, allow, *, connect=None, **options):
        async def elsewhere(address, port):
            if address == "93.184.215.14":
                reader = asyncio.StreamReader()
                reader.feed_eof()
                return reader, _Sink()
            return await connect(address, port)

        real(self, allow, connect=elsewhere, **options)

    monkeypatch.setattr(eg.Proxy, "__init__", init)
    checks = await example_41("memory", seed=3)
    assert checks[REBOUND][0] is False and "0 byte(s) back" in checks[REBOUND][1], checks[REBOUND]


# =================================================================================================
# AC-105: each check fails without its subject
# =================================================================================================


def without(monkeypatch, subject: str) -> None:
    """Takes `subject` away, as AC-105 names it."""
    if subject == "internal network":
        real = DockerProvider._cli

        async def cli(self, *args, **options):
            if args[:2] == ("network", "create"):
                kept, skip = [], False
                for arg in args:
                    if skip:
                        skip = False
                    elif arg == "--internal":
                        pass
                    elif arg == "-o":
                        skip = True
                    else:
                        kept.append(arg)
                args = tuple(kept)
            return await real(self, *args, **options)

        monkeypatch.setattr(DockerProvider, "_cli", cli)
    elif subject in ("address check", "SNI check"):
        line = ("            if not all(self.is_global(address) for address in addresses):" if subject == "address check"
                else "            if sni != host:")
        assert eg.SOURCE.count(line + NL) == 1, subject
        monkeypatch.setattr(eg, "SOURCE", eg.SOURCE.replace(line + NL, "            if False:" + NL))
    elif subject == "server's container removal":
        real = DockerProvider.remove

        async def spares_servers(self, handle):
            if "agentsdk.runner" in handle.labels and "agentsdk.run" not in handle.labels:
                return None
            return await real(self, handle)

        monkeypatch.setattr(DockerProvider, "remove", spares_servers)
    elif subject == "snapshot's tenancy check":
        async def any_tenancy(self, snapshot_id, tenant_id, project_id):
            return None

        monkeypatch.setattr(mgr.WorkspaceManager, "usable_snapshot", any_tenancy)
    elif subject == "compute charge":
        monkeypatch.setattr(api._Compute, "charge", lambda self, cpu_usec: None)
    else:
        raise AssertionError(subject)


NAMED = {"internal network": DIRECT, "address check": REFUSED, "SNI check": REFUSED,
         "server's container removal": KILLED, "snapshot's tenancy check": TENANCY, "compute charge": CHARGED}
# The clause each removal fails, from the example's own wording: the check fails for its subject's
# reason, not by another clause that happens to fail with it (M47 mutation run).
BECAUSE = {"internal network": lambda why: why.startswith("direct: ") and "connected" in why,
           "address check": lambda why: "upstream_failed" in why,
           "SNI check": lambda why: why.startswith("the refusals recorded were") and "sni_mismatch" not in why,
           "server's container removal": lambda why: "remained" in why,
           "snapshot's tenancy check": lambda why: why.startswith("it made ['"),
           "compute charge": lambda why: why.endswith("was charged None")}
BOTH = ["snapshot's tenancy check", "compute charge"]


def fails_for_its_subject(checks: dict, subject: str) -> None:
    passed, why = checks[NAMED[subject]]
    assert passed is False and BECAUSE[subject](why or ""), (subject, passed, why)


@pytest.mark.parametrize("subject", BOTH)
async def test_on_the_fake_each_check_fails_without_its_subject(monkeypatch, subject):
    without(monkeypatch, subject)
    fails_for_its_subject(await example_41("memory", seed=3), subject)


async def test_without_the_compute_charge_the_child_is_never_stopped(monkeypatch):
    without(monkeypatch, "compute charge")
    checks = await example_41("memory", seed=3)
    assert checks[STOPPED][0] is not True, checks[STOPPED]


async def test_stopped_fails_when_a_command_starts_after_the_ceiling(monkeypatch):
    """M47 round 1 F2: the burner's lease says yes twice more past its ceiling -- one model call, one
    command -- so a command starts after the crossing; STOPPED names that, not only what it spent."""
    from agentsdk.budget import BudgetLease

    real, hits = BudgetLease.may_call, []

    def may_call(self):
        answer = real(self)
        if not answer and self.node_id == "burn" and len(hits) < 2:
            hits.append(self.node_id)
            return True
        return answer

    monkeypatch.setattr(BudgetLease, "may_call", may_call)
    passed, why = (await example_41("memory", seed=3))[STOPPED]
    assert len(hits) == 2 and passed is False and why == "1 command(s) started after it", (hits, why)


async def test_tenancy_fails_when_the_other_tenancy_can_start_nothing(monkeypatch):
    """M47 round 1 F1 (DECISION-ee543c57): the other tenancy's restore refused for any reason passed;
    a refusal that holds for every workspace of that tenancy is no tenancy check."""
    real, hits = FakeProvider.create, []

    async def create(self, spec, labels):
        if labels.get("agentsdk.tenant", "").endswith(EX41.ELSEWHERE):
            hits.append(spec.from_snapshot)
            raise mgr.WorkspaceUnavailable("an unrelated failure")
        return await real(self, spec, labels)

    monkeypatch.setattr(FakeProvider, "create", create)
    passed, why = (await example_41("memory", seed=3))[TENANCY]
    assert hits and passed is False and why == "the other tenancy could start no workspace at all", (hits, why)


async def test_tenancy_fails_when_the_other_tenancy_may_release_the_snapshot(monkeypatch):
    real, hits = api.Runner.release_snapshot, []

    async def release(self, tenant_id, project_id, snapshot_id):
        if tenant_id.endswith(EX41.ELSEWHERE):
            hits.append(snapshot_id)
            return True
        return await real(self, tenant_id, project_id, snapshot_id)

    monkeypatch.setattr(api.Runner, "release_snapshot", release)
    passed, why = (await example_41("memory", seed=3))[TENANCY]
    assert hits and passed is False and why == "the other tenancy's release answered True", (hits, why)


async def test_killed_fails_when_the_tenancy_cannot_be_listed(monkeypatch):
    """M47 round 1 F3: a failed listing (None) read as nothing remaining."""
    hits = []

    async def unlisted(chosen, tenant):
        hits.append(tenant)
        return None

    monkeypatch.setattr(EX41, "remains", unlisted)
    passed, why = (await example_41("memory", seed=3))[KILLED]
    assert hits and passed is False and why == "the daemon could not be listed", (hits, why)


# =================================================================================================
# On a real daemon (pytest -m docker)
# =================================================================================================


@pytest.mark.docker
@pytest.mark.parametrize("seed", range(10))
@pytest.mark.parametrize("store", STORES)
async def test_on_docker_example_41_holds_every_check(store, seed):
    """FR-171: ten times on each store, the children's start order shuffled and their calls delayed
    at random. One run per test, under faulthandler_timeout: ten in one fired the stall dump on every
    gate (as two did in M44, DECISION-f7114b8b)."""
    checks = await example_41(store, "docker", seed=seed)
    assert len(checks) == DOCKER_CHECKS and not failed(checks), (len(checks), failed(checks))


@pytest.mark.docker
@pytest.mark.parametrize("subject", list(NAMED))
async def test_on_docker_each_check_fails_without_its_subject(monkeypatch, subject):
    without(monkeypatch, subject)
    fails_for_its_subject(await example_41("memory", "docker", seed=5), subject)


@pytest.mark.docker
async def test_on_docker_the_servers_check_holds_when_the_egress_child_does_nothing(monkeypatch):
    """M47 live run: the controls ran only after the egress probe, so a live egress child that ran
    out of output failed the server's check for want of them. Each check reads them, whatever
    another child did."""
    real = EX41.ScriptedChild.send

    async def egress_declines(self, request):
        response = await real(self, request)
        if any(EX41.runs_probe(c.arguments.get("argv"), "egress") for c in response.message.tool_calls):
            return EX41._reply(content="I will not run that.")
        return response

    monkeypatch.setattr(EX41.ScriptedChild, "send", egress_declines)
    checks = await example_41("memory", "docker", seed=6)
    assert checks[CONTAINED][0] is True, checks[CONTAINED]
    assert checks[ALLOWED][0] is None, checks[ALLOWED]


@pytest.mark.docker
def test_example_41_runs_scripted_on_docker():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "41_increment2_contention.py"), "--scripted"],
                          capture_output=True, text=True, timeout=1800, cwd=REPO)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
    assert done.stdout.count("[PASS]") == DOCKER_CHECKS and "FAIL" not in done.stdout
