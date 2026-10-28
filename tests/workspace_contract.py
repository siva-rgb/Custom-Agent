"""FR-139: the behaviour every workspace provider shares, written once.

Each `contract_*` coroutine takes a provider, a `Vocabulary` -- how that provider spells a few
commands -- and a `WorkspaceSpec`. tests/test_workspace.py runs them against `FakeProvider` in the
gates; from M39 tests marked `docker` run them against `DockerProvider` (DECISION-31b2c79d).
Not a test module itself: pytest collects only `test_*.py`.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
import time
from dataclasses import dataclass
from typing import Any, Callable

from agentsdk.errors import WorkspaceUnavailable
from agentsdk.workspace import CommandOutcome

LABELS = {"agentsdk.tenant": "SYN-contract", "agentsdk.project": "p-contract"}


@dataclass(frozen=True)
class Vocabulary:
    """How one provider spells the commands the contract runs. Each returns an argv."""

    say: Callable[[str], list[str]]  # prints its text and a newline to stdout, exits 0
    fail: Callable[[int], list[str]]  # prints "failed" to stderr, exits with the code
    nap: Callable[[float], list[str]]  # sleeps, then exits 0
    fill: Callable[[str, int], list[str]]  # writes that many bytes to the path, exits 0
    flood: Callable[[int], list[str]]  # prints that many bytes to stdout, exits 0
    show: Callable[[str], list[str]]  # prints the file, exits 0
    echo_stdin: Callable[[], list[str]]  # prints its stdin, exits 0
    environ: Callable[[], list[str]]  # prints NAME=value lines of its environment
    unknown: list[str]  # a command no image has


def regular_file_tar(path: str, data: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(path)
        info.size = len(data)
        info.uid = info.gid = 65534
        tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def members(data: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return {m.name.removeprefix("./"): m for m in tar.getmembers()}


def content_of(data: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for m in tar.getmembers():
            if m.name.removeprefix("./") == name and m.isfile():
                return tar.extractfile(m).read()
    raise AssertionError(f"{name} not in the export")


async def _fresh(provider, spec):
    return await provider.create(spec, dict(LABELS))


async def contract_a_new_workspace_is_empty(provider, words: Vocabulary, spec) -> None:
    handle = await _fresh(provider, spec)
    try:
        assert await provider.size(handle) == 0
    finally:
        await provider.remove(handle)


async def contract_a_command_returns_its_exit_code_output_and_duration(provider, words, spec) -> None:
    handle = await _fresh(provider, spec)
    try:
        ok = await provider.run(handle, words.say("hello"), timeout=30)
        assert isinstance(ok, CommandOutcome)
        assert (ok.exit_code, ok.stdout, ok.killed, ok.stdout_cut) == (0, b"hello\n", None, False), ok
        assert ok.duration >= 0
        bad = await provider.run(handle, words.fail(3), timeout=30)
        assert (bad.exit_code, bad.killed) == (3, None), bad
        assert b"failed" in bad.stderr
    finally:
        await provider.remove(handle)


async def contract_an_unknown_command_exits_127(provider, words, spec) -> None:
    handle = await _fresh(provider, spec)
    try:
        outcome = await provider.run(handle, words.unknown, timeout=30)
        assert (outcome.exit_code, outcome.killed) == (127, None), outcome
    finally:
        await provider.remove(handle)


async def contract_output_past_the_bound_is_cut_and_counted(provider, words, spec) -> None:
    handle = await _fresh(provider, spec)
    try:
        produced = spec.max_output_bytes * 3
        outcome = await provider.run(handle, words.flood(produced), timeout=60)
        assert outcome.exit_code == 0, outcome
        assert len(outcome.stdout) == spec.max_output_bytes and outcome.stdout_cut
        assert outcome.stdout_bytes == produced
    finally:
        await provider.remove(handle)


async def contract_a_command_past_its_timeout_is_killed(provider, words, spec) -> None:
    handle = await _fresh(provider, spec)
    try:
        started = time.monotonic()
        outcome = await provider.run(handle, words.nap(30), timeout=1.0)
        assert (outcome.exit_code, outcome.killed) == (None, "timeout"), outcome
        assert time.monotonic() - started < 15, "the timeout did not end the command"
    finally:
        await provider.remove(handle)


async def contract_a_killed_command_ends_as_cancelled(provider, words, spec) -> None:
    handle = await _fresh(provider, spec)
    try:
        running = asyncio.ensure_future(provider.run(handle, words.nap(30), timeout=60, command_id="c-kill"))
        await asyncio.sleep(1.5)
        started = time.monotonic()
        await provider.kill(handle, "c-kill")
        outcome = await asyncio.wait_for(running, 15)
        assert (outcome.exit_code, outcome.killed) == (None, "cancelled"), outcome
        assert time.monotonic() - started < 15
    finally:
        await provider.remove(handle)


async def contract_stdin_reaches_the_command(provider, words, spec) -> None:
    handle = await _fresh(provider, spec)
    try:
        outcome = await provider.run(handle, words.echo_stdin(), timeout=30, stdin=b"from the host")
        assert outcome.stdout == b"from the host", outcome
    finally:
        await provider.remove(handle)


async def contract_a_command_sees_only_the_specs_environment(provider, words, spec) -> None:
    handle = await _fresh(provider, spec)
    try:
        outcome = await provider.run(handle, words.environ(), timeout=30)
        seen = dict(line.split("=", 1) for line in outcome.stdout.decode().splitlines() if "=" in line)
        for name, value in spec.env.items():
            assert seen.get(name) == value, (name, seen)
        # Nothing of the host's: none of its variable names is set with the host's value.
        import os

        leaked = {k for k, v in os.environ.items() if seen.get(k) == v and k not in spec.env and v}
        assert not leaked, f"host variables reached the command: {sorted(leaked)}"
    finally:
        await provider.remove(handle)


async def contract_files_round_trip_and_count_towards_the_size(provider, words, spec) -> None:
    handle = await _fresh(provider, spec)
    try:
        await provider.import_(handle, regular_file_tar("notes/a.txt", b"payload"))
        assert (await provider.run(handle, words.show("notes/a.txt"), timeout=30)).stdout == b"payload"
        await provider.run(handle, words.fill("big.bin", 4096), timeout=30)
        assert await provider.size(handle) >= 4096 + len(b"payload")
        exported = await provider.export(handle, "notes/a.txt")
        assert content_of(exported, "a.txt") == b"payload"
    finally:
        await provider.remove(handle)


async def contract_an_import_holding_anything_but_regular_files_is_refused(provider, words, spec) -> None:
    handle = await _fresh(provider, spec)
    try:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            link = tarfile.TarInfo("link")
            link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(link)
        try:
            await provider.import_(handle, buffer.getvalue())
        except ValueError:
            pass
        else:
            raise AssertionError("a symlink was imported")
        assert await provider.size(handle) == 0
    finally:
        await provider.remove(handle)


async def contract_a_missing_path_cannot_be_exported(provider, words, spec) -> None:
    handle = await _fresh(provider, spec)
    try:
        try:
            await provider.export(handle, "nothing-here")
        except FileNotFoundError:
            return
        raise AssertionError("a missing path was exported")
    finally:
        await provider.remove(handle)


async def contract_a_removed_workspace_runs_nothing(provider, words, spec) -> None:
    handle = await _fresh(provider, spec)
    await provider.remove(handle)
    try:
        await provider.run(handle, words.say("x"), timeout=30)
    except WorkspaceUnavailable:
        return
    raise AssertionError("a removed workspace ran a command")


async def contract_owned_workspaces_are_listed_by_their_labels(provider, words, spec) -> None:
    mine = await provider.create(spec, {**LABELS, "agentsdk.run": "contract-a"})
    other = await provider.create(spec, {**LABELS, "agentsdk.run": "contract-b"})
    try:
        found = await provider.list_owned({"agentsdk.run": "contract-a"})
        assert [h.workspace_id for h in found] == [mine.workspace_id], found
    finally:
        await provider.remove(mine)
        await provider.remove(other)


CONTRACT: dict[str, Any] = {
    name.removeprefix("contract_"): fn for name, fn in dict(globals()).items() if name.startswith("contract_")
}
