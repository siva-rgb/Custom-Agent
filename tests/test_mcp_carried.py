"""M33 gate, first half: the items carried from M32's review (FR-122, AC-89).

KNOWLEDGE-d2afcab0 and DECISION-e75ad00a: example 26's rejection check names its subject; its
live checks see a skipped approval; the write tools' sweep compares names as NTFS does; and
example 23's live checks say when they saw nothing.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from agentsdk.builtin_tools import write_file_tool
from agentsdk.config import normalise_database_url
from agentsdk.tools import ApprovalPolicy

REPO = Path(__file__).resolve().parent.parent
DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
TENANT, PROJECT = "SYN-m33c", "p-m33c"


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE tenant_id = %s", (TENANT,))]
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


def example(name):
    spec = importlib.util.spec_from_file_location(Path(name).stem, REPO / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REJECTED = "b.txt was rejected by a person, and its reason reached its writer"
EVERY_PATH = "every scripted path but late.txt was decided by a person or a standing decision"


async def failed_labels(module, **patches):
    for name, value in patches.items():
        setattr(module, name, value)
    checks, _ = await module.offline("memory", seed=0, quiet=True, tenant=TENANT, project=PROJECT)
    return [label for label, passed in checks if not passed]


@pytest.mark.skipif(sys.platform != "win32", reason="the write tools are Windows-only (D11)")
async def test_example_26s_rejection_check_needs_b_even_when_the_race_reject_has_a_reason():
    module = example("26_approval_contention.py")

    async def with_reasons(runner, tenant, project, interruption_id):
        """The race, each person giving a reason, the rejecting one first: a reasoned rejection
        that is not b.txt's exists, and must not stand in for it."""
        outcomes = []
        for index in (0, 1):
            try:
                await runner.decide(tenant, project, interruption_id, approve=index == 1,
                                    principal=module.APPROVERS[index], message=f"person {index}")
                outcomes.append("recorded")
            except module.InterruptionAlreadyDecided:
                outcomes.append("refused")
        return {"outcomes": sorted(outcomes), "interruption": interruption_id}

    failed = await failed_labels(module, UNDECIDED={"late.txt", "b.txt"}, decide_twice=with_reasons)
    assert REJECTED in failed and EVERY_PATH in failed, failed


@pytest.mark.skipif(sys.platform != "win32", reason="the write tools are Windows-only (D11)")
@pytest.mark.parametrize("path", ["a.txt", "base.txt", "e.txt"])
async def test_example_26s_shared_checks_see_a_skipped_path(path):
    """The checks example 26 runs live are pure functions of its records: run over an offline
    run whose screen skips one path, they fail."""
    module = example("26_approval_contention.py")
    assert EVERY_PATH in await failed_labels(module, UNDECIDED={"late.txt", path})


@pytest.mark.skipif(sys.platform != "win32", reason="the write tools are Windows-only (D11)")
async def test_the_sweep_compares_names_as_ntfs_does(tmp_path):
    """Python folds the Kelvin sign to k; NTFS does not, so its orphan is another target's."""
    old = time.time() - 3600
    kelvin = tmp_path / f".K.txt.agentsdk-{uuid.uuid4()}.tmp"
    upper = tmp_path / f".K.TXT.agentsdk-{uuid.uuid4()}.tmp"
    for path in (kelvin, upper):
        path.write_text("stale", encoding="utf-8")
        os.utime(path, (old, old))
    await write_file_tool(tmp_path, approval_policy=ApprovalPolicy.AUTO).fn(path="k.txt", content="x")
    assert kelvin.exists() and not upper.exists()


def test_example_23s_live_checks_report_what_they_did_not_see():
    module = example("23_gate_contention.py")
    checks = [("seen", True), ("a lost race", None)]
    lines, failed = module.report(checks, live=True)
    assert lines == ["[PASS] seen", "[SKIP] not exercised: a lost race"] and failed == []
    lines, failed = module.report(checks, live=False)
    assert lines == ["[PASS] seen", "[FAIL] a lost race"] and failed == ["a lost race"]
