"""M8 gate: the SDK as a standalone project (FR-22..FR-25, NFR-9, NFR-10, AC-16..AC-19).

Every example in scripts/ runs as a subprocess from a copy OUTSIDE the
repository, with every model and database variable removed, so an example that
quietly needs credentials, a checkout or a .env fails here rather than on a
reader's machine. The wheel is built from a copy too, so building never leaves
build/ or egg-info debris in the repository.
"""

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.parse

import pytest
from dotenv import dotenv_values

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
CONFIG_VARS = ("BASE_URL", "MODEL_API_KEY", "DATABASE_URL", "DEFAULT_MODEL", "OTEL_EXPORTER_OTLP_ENDPOINT")
EXPECTED_EXAMPLES = {
    "01_minimal_agent.py",
    "02_custom_tools.py",
    "03_permissions_and_hooks.py",
    "04_persistence_and_trace.py",
    "05_switching_models.py",
    "06_mcp_tools.py",
    "07_delegating_to_a_child_run.py",
    "08_testing_agents_offline.py",
    # FR-56 as amended 2026-09-15 (DECISION-40ae2d24): 09 to 12 in M13; 13 with M14.
    "09_limits_and_cost.py",
    "10_builtin_tools.py",
    "11_run_handle.py",
    "12_artifacts.py",
    "13_telemetry.py",
    # FR-66 (M16): the plan as a persisted object.
    "14_plan.py",
    # FR-69 (M17): the budget governor.
    "15_budget.py",
    # FR-72 (M18): subagents under a run's limits.
    "16_subagents.py",
    # FR-75 (M19): the orchestrator, a failed criterion and the replan.
    "17_orchestrator.py",
    "18_context.py",
    # FR-91 (M22): source versions and the scoped resource cache.
    "19_sources.py",
    # FR-94 (M23): the evidence ledger.
    "20_ledger.py",
    # FR-96 (M24): the exit signal, claim-lock under contention.
    "21_contention.py",
    # FR-106 (M27): the critic.
    "22_critic.py",
    # FR-108 (M28): the exit signal under the gate.
    "23_gate_contention.py",
    # FR-117 (M30): approvals.
    "24_approvals.py",
    # FR-119 (M31): the write tools.
    "25_write_tools.py",
    # FR-121 (M32): approvals under contention, the exit signal.
    "26_approval_contention.py",
    # FR-126 (M33): native MCP over stdio.
    "27_mcp.py",
    "28_mcp_catalog.py",
    # FR-132 (M35): Streamable HTTP, resources and prompts.
    "29_mcp_http.py",
    # FR-134 (M36): input requests and elicitation.
    "30_mcp_input.py",
    # FR-135 (M37): hostile MCP servers under orchestration, Phase 4's exit signal.
    "31_mcp_contention.py",
    # FR-141 (M38): a workspace, on the fake provider.
    "32_workspace.py",
    # FR-145 (M39): the Docker provider.
    "33_docker_workspace.py",
    # FR-149 (M40): timeouts and cancellation.
    "34_sandboxed_tools.py",
    "35_workspace_plan.py",
    # FR-154 (M42): Phase 5's exit signal.
    "36_sandbox_contention.py",
}


def _pins(lines):
    """Requirement specifiers from requirements-style lines, comments dropped."""
    pins = set()
    for line in lines:
        spec = re.split(r"\s+#", line, maxsplit=1)[0].strip()
        if spec and not spec.startswith("#"):
            pins.add(spec)
    return pins


def _pyproject():
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _offline_env():
    env = {k: v for k, v in os.environ.items() if k not in CONFIG_VARS}
    env["PYTHONPATH"] = str(REPO)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_offline(script, cwd, timeout=240):
    return subprocess.run(
        [sys.executable, str(script), "--offline"],
        cwd=cwd,
        env=_offline_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def isolated_scripts(tmp_path_factory):
    assert SCRIPTS.is_dir(), "scripts/ does not exist"
    dest = tmp_path_factory.mktemp("outside-repo") / "scripts"
    shutil.copytree(SCRIPTS, dest, ignore=shutil.ignore_patterns("__pycache__"))
    # The isolation is itself a claim, so check it: a .env anywhere above the
    # copy would let an example read credentials it was supposed to lack.
    for directory in (dest, *dest.parents):
        assert not (directory / ".env").exists(), f"a .env exists at {directory}"
    return dest


# --- FR-23, FR-24, AC-17: the examples -----------------------------------------


def test_the_eight_example_activities_exist():
    """FR-23 names eight activities; each is one file."""
    present = {p.name for p in SCRIPTS.glob("*.py")} if SCRIPTS.is_dir() else set()
    missing = EXPECTED_EXAMPLES - present
    assert not missing, f"FR-23 examples missing from scripts/: {sorted(missing)}"


def test_every_example_runs_offline_outside_the_repository(isolated_scripts):
    """AC-17. Runs EVERY .py in scripts/, not a list of them, so a script added
    later without an offline mode fails here instead of going unexercised."""
    scripts = sorted(isolated_scripts.glob("*.py"))
    assert len(scripts) >= len(EXPECTED_EXAMPLES), f"only {len(scripts)} scripts to run"
    failures = {}
    for script in scripts:
        proc = _run_offline(script, cwd=isolated_scripts.parent)
        if proc.returncode != 0:
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-8:]
            failures[script.name] = (proc.returncode, tail)
    assert not failures, "examples failed offline:\n" + "\n".join(
        f"  {name}: exit {code}\n      " + "\n      ".join(tail)
        for name, (code, tail) in failures.items()
    )


def test_the_mcp_example_marks_bridged_results_untrusted(isolated_scripts):
    """AC-18 and FR-25: a real local MCP server, a real SDK run, and every result
    the bridge produced carrying untrusted MCP provenance in the run history."""
    proc = _run_offline(isolated_scripts / "06_mcp_tools.py", cwd=isolated_scripts.parent)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2500:]

    offered = re.search(r"^MCP server offered: (.+)$", proc.stdout, flags=re.M)
    assert offered and offered.group(1).strip(), (
        "the example did not report tools listed by a real MCP server"
    )
    reported = re.findall(
        r"^provenance: tool=(\S+) origin=(\S+) trust_zone=(\S+) instruction_authority=(\S+)$",
        proc.stdout,
        flags=re.M,
    )
    assert reported, "no tool-result provenance was reported from the run history"
    for tool, origin, zone, authority in reported:
        assert (origin, zone, authority) == ("mcp_resource", "untrusted", "data_only"), (
            f"{tool}: origin={origin} trust_zone={zone} instruction_authority={authority}"
        )

    source = (SCRIPTS / "06_mcp_tools.py").read_text(encoding="utf-8")
    # Edited at M33 (FR-25 as amended by FR-126): the example points to native support.
    assert "bridges by hand" in source and "agentsdk.mcp" in source, "FR-25: the example must say what it is"


# --- FR-22, AC-16: packaging -------------------------------------------------------


def test_a_built_wheel_imports_from_outside_the_repository_with_its_schema(tmp_path):
    """AC-16. Built from a COPY, installed into a directory outside the
    repository, and imported from there -- then asked for the schema and the
    migrations through the SDK's own paths, which is what apply_schema uses."""
    assert (REPO / "pyproject.toml").is_file(), "pyproject.toml does not exist"
    src = tmp_path / "src"
    shutil.copytree(REPO / "agentsdk", src / "agentsdk", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(REPO / name, src / name)

    dist = tmp_path / "dist"
    built = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(src), "--no-deps", "--no-build-isolation", "-w", str(dist)],
        cwd=tmp_path, capture_output=True, text=True, timeout=300,
    )
    assert built.returncode == 0, built.stdout[-1500:] + built.stderr[-1500:]
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"

    site = tmp_path / "site"
    installed = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(site), str(wheels[0])],
        cwd=tmp_path, capture_output=True, text=True, timeout=300,
    )
    assert installed.returncode == 0, installed.stdout[-1500:] + installed.stderr[-1500:]

    probe = (
        "import agentsdk\n"
        "from agentsdk.migrate import discover\n"
        "from agentsdk.postgres import SCHEMA_PATH\n"
        "print(agentsdk.__file__)\n"
        "print(SCHEMA_PATH.is_file())\n"
        "print(','.join(path.name for _, path in discover()))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(site)
    ran = subprocess.run(
        [sys.executable, "-c", probe], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert ran.returncode == 0, ran.stderr[-1500:]
    location, schema_shipped, migrations = ran.stdout.strip().splitlines()
    assert pathlib.Path(location).resolve().is_relative_to(site.resolve()), (
        f"agentsdk was imported from {location}, not from the installed wheel"
    )
    assert schema_shipped == "True", "schema.sql is not in the wheel"
    on_disk = ",".join(p.name for p in sorted((REPO / "agentsdk" / "migrations").glob("*.sql")))
    assert migrations == on_disk, f"the wheel ships [{migrations}]; the repository has [{on_disk}]"


def test_the_package_declares_exactly_the_runtime_dependencies_of_requirements_txt():
    """FR-22. Two lists of the same dependencies drift the first time someone
    edits one, so they are compared rather than trusted."""
    runtime = (REPO / "requirements.txt").read_text(encoding="utf-8").split("# --- test")[0]
    declared = set(_pyproject()["dependencies"])
    assert declared == _pins(runtime.splitlines()), (
        f"pyproject {sorted(declared)} != requirements.txt {sorted(_pins(runtime.splitlines()))}"
    )


# --- NFR-9, NFR-10: dependencies and documentation ----------------------------


def test_example_dependencies_stay_out_of_the_sdk():
    """NFR-9. Declared only as an extra, and never imported by the SDK itself --
    the second half is the one that matters, and only running it can show it."""
    project = _pyproject()
    assert not any(dep.lower().startswith("mcp") for dep in project["dependencies"])
    # Edited at M33 (FR-123): the pin moved to 2.3.0, and the `mcp` extra holds it too (NFR-34).
    assert "mcp==2.3.0" in project["optional-dependencies"]["examples"]
    # Edited at M35 (NFR-34, owner): the extra pins httpx2 too, the client its HTTP transport is given.
    assert project["optional-dependencies"]["mcp"] == ["mcp==2.3.0", "httpx2==2.12.0"]
    assert "mcp==2.3.0" in _pins((SCRIPTS / "requirements.txt").read_text(encoding="utf-8").splitlines())

    probe = (
        # Edited at M33 (NFR-34): agentsdk.mcp is imported too, and still loads no mcp module.
        "import sys, agentsdk, agentsdk.providers, agentsdk.postgres, agentsdk.migrate, agentsdk.mcp\n"
        "print(sorted(m for m in sys.modules if m in ('mcp', 'httpx2') or m.startswith(('mcp.', 'httpx2.'))))\n"
    )
    ran = subprocess.run(
        [sys.executable, "-c", probe], cwd=REPO, env=dict(os.environ, PYTHONPATH=str(REPO)),
        capture_output=True, text=True, timeout=120,
    )
    assert ran.returncode == 0, ran.stderr[-1500:]
    assert ran.stdout.strip() == "[]", f"importing the SDK loaded MCP modules: {ran.stdout.strip()}"


def test_the_readme_license_and_env_example_are_honest_and_complete():
    """NFR-10. The variables are read off the code rather than listed here, so a
    variable added to the SDK or an example without documenting it fails."""
    readme = (REPO / "README.md").read_text(encoding="utf-8").lower()
    for gap in ("streaming", "orchestration", "mcp", "sandbox", "approval", "compaction", "budget", "resume"):
        assert gap in readme, f"the README never mentions {gap!r} among what is not built"
    assert (REPO / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")

    read = set()
    for path in [*(REPO / "agentsdk").rglob("*.py"), *SCRIPTS.glob("*.py")]:
        text = path.read_text(encoding="utf-8")
        read |= set(re.findall(r"os\.environ\.get\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']", text))
        read |= set(re.findall(r"os\.environ\[\s*[\"']([A-Z][A-Z0-9_]+)[\"']\s*\]", text))
    assert read, "found no environment variables read by the code, so this checks nothing"

    example = {}
    for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            example[name.strip()] = value.strip()
    assert not read - set(example), f".env.example does not name {sorted(read - set(example))}"
    assert all(value == "" for value in example.values()), (
        f".env.example carries values: {sorted(k for k, v in example.items() if v)}"
    )


# --- AC-19: nothing committable carries a credential ---------------------------


def test_no_tracked_or_committable_file_contains_a_credential():
    """AC-19. Scans what git would commit -- tracked files AND untracked files
    that are not ignored -- because the gate runs before the commit does.
    Reports file names only, never the matched value."""
    env_file = REPO / ".env"
    assert env_file.is_file(), "AC-19 needs .env to know what to look for; it fails rather than skips"
    values = dotenv_values(env_file)
    key = (values.get("MODEL_API_KEY") or "").strip()
    database = urllib.parse.urlparse((values.get("DATABASE_URL") or "").strip())
    host = urllib.parse.urlparse((values.get("BASE_URL") or "").strip()).hostname or ""
    needles = {
        "MODEL_API_KEY": key,
        "MODEL_API_KEY prefix": key[:8],
        "database password": database.password or "",
        "gateway hostname": host,
    }
    needles = {label: value for label, value in needles.items() if len(value) >= 6}
    assert {"MODEL_API_KEY", "gateway hostname"} <= set(needles), (
        "the key or the gateway hostname is missing from .env, so this scan proves nothing"
    )

    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
    ).stdout.split("\0")
    files = [REPO / rel for rel in listed if rel and (REPO / rel).is_file()]
    assert files, "git listed no files"
    leaks = {}
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        found = [label for label, value in needles.items() if value in text]
        if found:
            leaks[str(path.relative_to(REPO))] = found
    assert not leaks, f"credential material found in: {leaks}"
