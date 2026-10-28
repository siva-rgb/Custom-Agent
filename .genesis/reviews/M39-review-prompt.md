You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Sonnet 5, falling back to Opus 5** if Sonnet 5 fails to
start. M39 was implemented by Opus 5.5 and its pre-review run by a Fable 5.1 subagent; M38 was
approved by Fable 5.1. Bring no memory of an earlier review into this one: if your session holds
any, say so and ask for a fresh one.

M39 is **Phase 5's second milestone**: the Docker provider, its hardening and file transfer.
Re-derive the requirements from SPEC.md, section "### Phase 5, increment 1":
- the study and the decisions table P5-D1 to P5-D15;
- "#### M39": FR-142 to FR-145, as amended at M39's pre-flight and pre-review;
- FR-138's protocol, which gains `list` (DECISION-0004d2dc), and FR-146's `list_workspace`;
- NFR-43 and NFR-45, and AC-97 (as amended).

Read the records:
- KNOWLEDGE-bdc63cbf: the Docker study the decisions rest on;
- KNOWLEDGE-1e7b2114, DECISION-94bfee7c: M38's approval, the caveats it carried into M39, and the
  owner's standing rule that **no check keys on an error message's text**;
- KNOWLEDGE-c0328243, DECISION-504c7f04: what the `docker` CLI does on this host, measured, and the
  owner's three pre-flight decisions (env values and `docker inspect`; create, start, inspect, rm;
  the disk bound read by `du`, failing closed);
- KNOWLEDGE-9505dd77, DECISION-0004d2dc: the pre-review's eleven findings, and the owner's two
  decisions on them (`list` in the protocol; a link in a parent folder declared).

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5`; HEAD is `3e611ca` (M38). M39 is uncommitted, with SPEC.md's M39 amendments. No
migration is added.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs:** the gate writes `.genesis/project.json` back when it
ends, and a record made meanwhile is lost.

- **Configuration:** DATABASE_URL, BASE_URL and MODEL_API_KEY from .env. **Never print credentials
  or a connection string;** on a database error print the exception's type only.
- **Host:** Windows 11 Home, Python 3.11. **This laptop enters Modern Standby on battery when
  idle,** which suspends a gate mid-run; run long commands under a wrapper holding
  `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`.
- **Docker Desktop is running** (engine 29.6.2 in WSL2). The tests and example 33 use only
  `python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df`, already on the
  host. **Never pull, prune, stop or restart anything.** Other projects' images, containers and
  volumes are on this host: touch only workspaces whose `agentsdk.tenant` label starts with your
  tenant prefix, and raw containers or volumes you make yourself labelled `agentsdk-probe`. Report
  how many of either remain at the end (it must be 0).

**Running tests**
- **One test session at a time.** `tests/conftest.py` refuses a second one with exit code 3.
- Check that no other python or node process is running first. Run probes with
  `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **The gates run `FakeProvider` only:** `pytest.ini` deselects tests marked `docker`. Run those
  live with `pytest tests/test_docker_provider.py -m docker -p no:cacheprovider` (about two
  minutes); AC-97 is held there.
- **Prefer runtime mutation,** leaving no repository file changed. Restore any edited file byte
  for byte, verified by SHA-256. **Check that no repository file changed by content, never by
  mtime.**
- **Timings:** the full suite takes about 22 minutes; `tests/test_docker_provider.py` about 15
  seconds without Docker; a Docker command about 0.5 s, a size reading 0.35 s more, creating and
  removing a workspace about 1.3 s.

**Gates:** `genesis gate . M39-docker-provider --timeout 1500000` in the background.

**Please complete every attack item, and verify the gate evidence yourself.**

**Cleanup order:** `workspace_commands`, then `workspaces`, then `interruption_decisions`,
`interruptions` and `standing_decisions`, then `artifacts` by `source_run`, `plan_node_states`,
`plan_versions`, `run_events`, `messages`, `execution_manifests`, then the runs, children first, by
run id. Use tenant ids starting `SYN-m39r1`, and remove only rows of runs your probes recorded.

## Task under review

**M39-docker-provider**, round 1, risk **high**.

### What M39 builds

**1. `DockerProvider` (FR-142),** `agentsdk/workspace/docker.py`. A workspace is a Docker volume
labelled with its tenant, project, run, workspace and owner, its root given to 65534 by
`docker cp -a` of a `workspace/` entry into a never-started container (no process runs). Each
command is its own container: `docker create` with exactly P5-D6's flags (`container_args`,
`hardening`), `--pull never`, `--entrypoint=` so the argv runs as given, `-e NAME` for each env
name with its value only in the CLI's own environment; then `docker start -a -i`; then
`docker inspect` for the exit code and `OOMKilled` (`killed="limit"`); then `docker rm -f`, in a
`finally` that a cancellation waits for. A timeout or `kill()` removes the container by name, which
works from the moment it exists. `remove()` kills its commands, removes every container labelled
with the workspace, then the volume. Env names the CLI reads, or two differing only in case, fail
`create` with `WorkspaceUnavailable` naming the variable. An argv past the Windows command line
ends as exit 126, `argument list too long`.

**2. Transfer (FR-143).** `read_tar` refuses a whole stream for any member but a regular file or
directory (naming it), a sparse member, an absolute or `..` path, a repeated path, a file that is
also a folder, over 16 MiB (bytes or stored sizes) or over 10,000 members. `import_tar` rebuilds
what goes in under `workspace/`, every parent given, owned by 65534. `export` reads `docker cp`'s
stream, ending the copy the moment it passes 16 MiB, and tells a missing path
(`FileNotFoundError`) from a failed copy by whether the helper container still exists, never by
docker's message.

**3. The measured bounds (FR-144).** `size()` is `du -sk` in a hardened container minus the empty
root's own use, read at creation. The manager now holds a failed or timed-out reading past the
bound (`RunWorkspace._measure`, `_unread`), so the next command and write are refused
`WorkspaceRefused`, with `unread` in the `WorkspaceRefused` event.

**4. Listing (DECISION-0004d2dc).** The protocol gains `list(handle, path, limit)`.
`FakeProvider` reads its tree; `DockerProvider` runs `_LIST` with `sh` and `stat` in a hardened
container. `list_workspace` uses it; `tools.entries` is gone.

**5. M38's carried caveats.** `FakeProvider.remove` kills before it forgets; a contract case
holds removal killing a running command on both providers. Example 32's untrusted check and
refusal check read each call's `error_type` from its `ToolCalled` event. Two comments no longer
call FR-144 M38's.

**6. Example 33 (FR-145)** runs example 32's agent on `DockerProvider`, then shows the hardening
from inside a command and an exported file. `--offline` runs on `FakeProvider` (the gates);
`--scripted` on Docker with a scripted model; live without either. Without Docker it prints
`[SKIP] docker not running`.

**Choices the author made that the SPEC leaves open** (judge each):
- `pytest.ini` registers the `docker` marker and deselects it by default, so the gates never need
  a daemon (DECISION-31b2c79d); `pytest -m docker` replaces the selection.
- The helper, measurement and listing containers carry the workspace's labels, so `remove()` and
  reaping find them; the listing and measurement containers get no `WorkspaceSpec.env`.
- `create` asks `docker version` first, so a stopped daemon is named as such.
- A workspace volume also carries `agentsdk.spec`, the spec's record without env or approval, so
  `list_owned` can rebuild a handle; a label it cannot read gives `_UNKNOWN_SPEC`.
- The new rule (DECISION-94bfee7c) is applied to every check M39 writes or touches. Earlier
  phases' approved tests that assert a result's `ClassName:` text were not rewritten.

### Every finding so far, answered

A missing or false row is itself a finding.

| Source | Finding | Answer |
|---|---|---|
| M38 review | `FakeProvider.remove` popped the workspace, then could not find it to kill | Fixed: kills first. Contract case `removing_a_workspace_kills_its_running_commands`, both providers |
| M38 review | Example 32's live untrusted check excluded results by text prefix; a real rejection reads `rejected...` | Fixed: by `ToolCalled.error_type`. `test_example_32_judges_a_rejected_call_by_its_error_class_not_its_text` |
| M38 review | Two comments called FR-144 M38's | Fixed |
| M38 review, smaller | `FakeFiles.untar` checks conflicts against the tree, not within one stream | Not changed in the fake; `read_tar` (Docker) refuses them within a stream (P7) |
| M38 review, smaller | A README row promised removing files, which nothing can do | Reworded: the workspace stays refused for the rest of its run |
| M38 review, smaller | An approver approves a `run_command` a full workspace then refuses; `_end` awaits creation unbounded | Carried: M40's (approval and cancellation) |
| Pre-flight | Volume root owned by root, so 65534 cannot write | `docker cp -a` of `workspace/` into `/` (KNOWLEDGE-c0328243) |
| Pre-flight | `-e NAME` leaves values in inspect's `Config.Env` | Owner: AC-97 amended, deny-list (DECISION-504c7f04) |
| Pre-flight | `docker run --rm`: kill race, 125/127 ambiguity, no OOM | Owner: create/start/inspect/rm (DECISION-504c7f04) |
| Pre-flight | M38's manager let commands go on after a failed size reading | Owner: fail closed. `test_a_workspace_whose_size_cannot_be_read_refuses_every_later_command_and_write`, both stores; live `test_a_folder_the_measurement_cannot_read_refuses_the_next_command` |
| Mutation | Nothing held the export bound applied while reading | `test_an_export_that_never_ends_is_cut_at_its_bound_while_it_is_read` |
| Mutation | Nothing held an OOM kill as `limit` | `test_a_command_past_its_memory_ends_killed_by_the_limit` (docker) |
| Pre-review P1 | `list_workspace` of a sub-folder named the folder itself | Owner: `list` in the protocol (DECISION-0004d2dc). Contract case `a_folder_is_listed_by_its_direct_entries`; `test_list_workspace_names_a_sub_folders_entries` |
| Pre-review P2 | A link in a parent folder is followed by `docker cp` | Owner: declared (DECISION-0004d2dc). `test_a_link_in_a_parent_folder_reaches_only_the_images_files` (docker) |
| Pre-review P3 | A folder with a link, FIFO, over 16 MiB or 10,000 files could not be listed | As P1. `test_a_folder_holding_links_fifos_and_more_than_a_transfer_is_listed_as_it_is` (docker) |
| Pre-review P4 | An argv past the Windows command line was reported as docker missing | Exit 126; `_spawn` refuses an over-long command line; `list_owned` inspects 100 volumes a call. `test_an_argv_past_the_command_line_limit_is_the_commands_failure_not_dockers` |
| Pre-review P5 | One damaged `agentsdk.spec` label failed `list_owned` | `_UNKNOWN_SPEC`. `test_a_volume_whose_labels_name_no_readable_spec_is_still_listed` |
| Pre-review P6 | A stopped daemon was reported as a missing image | `docker version` first. `test_an_unreachable_daemon_is_named_as_such_not_as_a_missing_image` |
| Pre-review P7 | A sparse member expanded to 256 MiB; a stream could name a path twice or as file and folder | Refused. `test_a_sparse_member_a_repeated_path_or_a_file_that_is_also_a_folder_fails_the_transfer` |
| Pre-review P8 | A name holding `:` cannot be exported on a Windows host | Declared (README) |
| Pre-review P9 | `kill -9 $$` from `sh -c` exits 0 (PID 1 ignores its own signals) | Noted: the kernel's, not the provider's |
| Pre-review P10 | A flooding command's timeout returns in about 2.5 times the timeout | Noted: within the executor's deadline |
| Pre-review P11 | Go runtime variables (`GODEBUG` and others) not on the deny-list | Added. In the refusal test's parameters |

### How the author checked it

- **Tests first:** `tests/test_docker_provider.py` and the contract additions were written before
  the code they hold, then run against it.
- **Mutation:** 41 of 41 killed by named tests, each file restored by SHA-256: 30 against the
  first build, and 11 that revert each pre-review repair.
- **Gates,** on source hash `8d6b2cd7`:
  - `unit`: 34 passed, 26 deselected (the docker-marked tests) in 15.15s;
  - `regression`: 2774 passed, 26 deselected in 1324.31s.

  The first gate run left `unit` stale only through `.pytest_cache`; both were run again and
  pass on the one hash.
- **Docker, live, on the final code:** `pytest tests/test_docker_provider.py -m docker`: 26 passed
  in 100.39s -- every contract case on `DockerProvider`, the hardening read from inside a command,
  the env and `docker inspect`, links, FIFOs and oversized exports, listing, a parent link, the
  OOM kill, output cut at 20 MB, the disk bound on both stores, and a folder `du` cannot read.
- **Live examples,** against the gateway model on PostgreSQL, after the gates:
  - example 32: its four live checks PASS;
  - example 33 `--scripted`: 13 of 13 PASS on Docker;
  - example 33 live: 10 of 10 PASS. The model ran the whole task on Docker: `wc` read 4 words, `cat`
    exited 1, `dd` wrote 8000 B, and its next command was refused (12288 B past 6000 B).
  - **Cleanup:** both runs' rows removed by their printed run ids, the tenancy's catalog and
    artifact rows counted first (0). The store is at the baseline: 10351 runs, 19 under
    example-tenant, 0 SYN, none running, 0 artifacts, empty catalog, interruption and workspace
    tables. No container or volume labelled `agentsdk.workspace` remains.

### Edits to approved tests

- **`tests/workspace_contract.py`:** two new cases, `removing_a_workspace_kills_its_running_commands`
  (M38's review) and `a_folder_is_listed_by_its_direct_entries` (DECISION-0004d2dc). No existing
  case is changed.
- **`tests/test_distribution.py`:** `EXPECTED_EXAMPLES` gains `33_docker_workspace.py`.
- **`pytest.ini`:** the `docker` marker and `addopts = -m "not docker"`.
- No other approved test is edited. NFR-16 and NFR-38: M39 adds no table, so `tests/conftest.py`'s
  `_TRACKED` is unchanged.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Probes in `m39-r1-probes` in your own scratchpad.

0. **The table above:** check every row is true, and every approved-test edit by content.
1. **Hardening:** a command reaching the network, the host, a capability, root, the image's root
   file system, or past its memory, CPU, process or `/tmp` limit; any flag a model or spec can
   reach. Observe from inside a command, not from the flags alone.
2. **NFR-45:** an env value on any `docker` command line, in an exception, a label, inspect's
   Cmd or Args, a record, or after its container is gone -- failure and cancellation paths too.
3. **Transfer:** a link, hard link, FIFO, device, absolute or `..` path, sparse member, or a
   stream over a bound crossing either way; the host opening a path inside a volume.
4. **Lifetimes:** a container outliving its command's timeout, kill, cancellation or its
   workspace's removal; a volume outliving `remove()`; races between kill, remove, cancel and
   `docker create`.
5. **The disk bound:** a command leaving more disk use than `size()` reports without the
   fail-closed path refusing the next command.
6. **Listing:** a listing wrong for any folder a model can make, or one that transfers contents.
7. **Failure honesty:** a command's own exit read as docker's failure or the reverse; a daemon or
   CLI failure named wrongly; a check anywhere in M39 keyed on an error's text (DECISION-94bfee7c).
8. **The contract:** every case on both providers; a behaviour the two providers differ on that
   the contract does not name.
9. **Regressions:** the full suite; example 32 offline and live; example 33 `--offline`,
   `--scripted` and live.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M38,** and the README's M39 rows.
- **Owner's choices:** DECISION-31b2c79d, DECISION-47158c90, DECISION-97fb116b, DECISION-bb81ebbe,
  DECISION-504c7f04, DECISION-0004d2dc.
- **M40 to M42's requirements are not M39's:** a cancelled run reporting cancelled only once its
  container is gone, bounded at 10 s (FR-148); `export_workspace_file`; plan and kept workspaces,
  `max_concurrent_workspaces`, `Runner.aclose()` removing workspaces of runs under way, reaping a
  dead owner's (FR-151, FR-152); the exit signal.
- **Examples read private state** as earlier ones do: `runner._sessions_for` for histories.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter; latent, out-of-scope or
  cosmetic findings are caveats in your reason.
- **Clean up** every run, row, container, volume, temp folder, process and lock you create, and say
  what you removed.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M39-docker-provider --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M39-docker-provider --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the ten attack items, and
your verdict.
