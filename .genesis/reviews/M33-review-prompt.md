You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Opus 5, falling back to Fable 5.1** if Opus 5 fails to
start. M33 was implemented by Opus 5.5. Earlier reviews: Sonnet 5 approved M32 round 3, after
Fable 5.1 rejected round 2 and Opus 5 approved round 1. Bring no memory of an earlier review into
this one: if your session holds any, say so and ask for a fresh one.

Re-derive the requirements from SPEC.md rather than inheriting the framing below:
- "### Phase 4, increment 2" and "#### M33": FR-122 to FR-126;
- AC-89 and AC-90;
- NFR-34, NFR-35, NFR-37, NFR-38 and NFR-39, which the task lists;
- FR-25 as amended, and FR-5, FR-40, FR-41 and FR-77, which M33 builds on.

Read the decision table P4-D16 to P4-D31 and the ten accepted choices (DECISION-c1be04a2).
Background: KNOWLEDGE-07544107 (the MCP study), KNOWLEDGE-9593410e (the spec review), and
KNOWLEDGE-d2afcab0 with DECISION-e75ad00a (M32's carried items, FR-122).

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-4`.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass `--help` to a genesis command:** it is read as an argument
and the command runs.

- **Configuration:** DATABASE_URL, BASE_URL and MODEL_API_KEY from .env. **Never print credentials
  or a connection string;** pass every DSN through `agentsdk.config.normalise_database_url`.
- **Host:** Windows with Developer Mode on (D7), Python 3.11. `mcp==2.3.0` is installed in the venv.

**Running tests**
- **One test session at a time.** `tests/conftest.py` holds an advisory lock for the session; a
  second session from another shell exits at once with code 3. If you see that, wait.
- Check that no other python or node process is running first. Run probes with
  `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **Prefer runtime mutation** (a monkeypatch in a subprocess, or a pytest plugin): it leaves no
  repository file changed. If you edit a file, restore it byte for byte and verify its SHA-256 in a
  `finally`; never `git stash`; never touch an applied migration (`0014` to `0016`).
- **MCP tests start real server processes.** A test that fails part-way can leave a server's
  sleeping child (`python -c "import time; time.sleep(600)"`) behind. Check for one after every run,
  and remove only those you can attribute to your own run, by command line.
- **The full suite takes about 9 minutes; the unit gate about 50 seconds.**

**Gates (DECISION-066e317a).** `genesis gate . M33-mcp-stdio-client --timeout 1500000` in the
background; then check for a stray python process. If a gate reads `stale` straight after, run the
gate files once with the cache on, then gate again.

**Cleanup order:** `interruption_decisions`, then `interruptions` and `standing_decisions`, then the
run's other rows, then the runs, children first, by run id; never by label.

## Task under review

**M33-mcp-stdio-client**, **round 1**, risk **high**. HEAD is `ca8783d` (M32). The signed spec of
increment 2 and its plan are uncommitted in the same working tree. This is the first milestone of
Phase 4 increment 2.

### FR-122: the carried items

- **Example 26's rejection check names its subject:** b.txt, rejected by a person, with its reason
  reaching its writer.
- **A new shared check,** live and offline: every scripted path except late.txt was decided by a
  person or a standing decision.
- **The sweep compares names as NTFS does.** `_FileSystem.same_name` binds `CompareStringOrdinal`
  with `bIgnoreCase`. `_temporary_of(name)` replaces the `_TEMPORARY` regex: the uuid is ASCII hex
  in any case, and the other parts are compared through `same_name`.
- **Example 23:** a check whose subject never happened is `None`. `report(checks, live)` prints
  `[SKIP] not exercised` live and fails offline.

### FR-123 to FR-126: native MCP over stdio

- **`agentsdk/mcp/__init__.py`, a new package:**
  - **`StdioServer`:** validated config, and `record()` giving names, never values.
  - **`McpClients`:** imports `mcp` when constructed, raising `ImportError` naming the extra.
    Connections are keyed by alias, tenant, project and event loop.
  - **`_Connection`:** the `mcp` `Client` lives in its own task, because anyio task groups must be
    left in the task that entered them. Its stderr goes to `os.devnull`.
    - **The listing** is read to the last page and capped at 1000 tools. It skips tools with a
      reason event: a bad name, one listed twice, allow and deny, a clash with a registered tool, a
      schema the validator refuses.
    - **Restarts:** only a successful open counts, and a second exit marks the connection dead.
  - **`RunTools`:** a per-run `ToolRegistry` whose tools are rebuilt each turn. `prepare()` runs
    before the manifest, and the first turn reuses that attempt.
- **`agentsdk/api.py`:**
  - `Runner(mcp_servers=...)`, `aclose()`, `async with`;
  - the run's view;
  - the manifest's `mcp_servers`;
  - the loop's `tools_refresh`.
- **Smaller changes elsewhere:**
  - `agentsdk/loop.py` calls `tools_refresh` before each request and emits its events;
  - `agentsdk/executor.py` validates arguments on a worker thread for an MCP-origin tool;
  - `agentsdk/permissions.py` lets `<alias>__*` in a profile allow that server's tools;
  - `agentsdk/events.py` adds `McpServerConnected`, `McpServerUnavailable` and `McpToolSkipped`;
  - `agentsdk/manifest.py`, `agentsdk/postgres.py` and migration `0016` add `mcp_servers`.
- **Packaging:** the `mcp` extra and `agentsdk.mcp` in `packages`, with the pins moved to
  `mcp==2.3.0`.
- **Examples:** `scripts/27_mcp.py`, and example 06's wording amended (FR-25).
- **Tests:**
  - **`tests/fixtures/mcp_servers.py`:** a modern server on the library's `MCPServer`, and a
    hand-written legacy server whose listing is hostile on purpose.
  - **`tests/test_mcp_carried.py` (6) and `tests/test_mcp_client.py` (34):** the unit gate.

### Clarifications the author made, to judge

1. **The library refuses a whole listing** when one tool's input schema type is not `object`
   (pydantic validation). The server is then reported unavailable, naming `ValidationError`.
   FR-125's schema skip applies to schemas the library accepts and the validator refuses.
2. **The library validates structured output itself,** on the event loop, raising `RuntimeError`.
   It is mapped to "output does not match its schema", and the client does not validate again.
   FR-125 says the client validates on a worker thread.
3. **Structured content joins the text only when there is no text block.** FR-125 says it is
   appended; but the modern server serialises it into text already, which would be duplicated.
4. **The inherited environment is the library's** `DEFAULT_INHERITED_ENV_VARS` (APPDATA, USERNAME,
   and others), not the five FR-123 names.
5. **The launcher escape (NFR-39).** On Windows a venv's `python.exe` is a launcher, and it starts
   the real interpreter before the library's Job Object holds the launcher. The real server, and any
   child it starts, can escape the job and outlive `aclose`. This is measured: under the launcher
   the modern fixture's child survived. The tests start servers with `sys._base_executable`, and
   the fixture loads the venv's packages itself (`FIXTURE_SITE`, `site.addsitedir`). Declared in
   the README.
6. **M33 has no catalog yet:** every listed tool is usable until M34. `approval` (`require` or
   `never`) is implemented now, from P4-D20; `by_annotations`, `trusted`'s effect and `accept` come
   at M34.
7. **FR-122 names `tests/test_gate_contention.py`.** Example 23's test is in
   `tests/test_calibration.py`, and it needed no edit: offline is unchanged.
8. **A server that cannot be reached is tried again each turn.** Only a successful start counts
   toward the single restart.

### Edits to approved tests

- **`tests/test_approval_contention.py`:** the `REJECTED` label, a new `EVERY_PATH`, and the
  expected failing labels of two meta-cases (FR-122).
- **`tests/test_distribution.py`:**
  - the `mcp` pins become 2.3.0, and the `mcp` extra is asserted;
  - example 06's FR-25 assertion follows its amended wording;
  - `EXPECTED_EXAMPLES` gains `27_mcp.py`;
  - the NFR-9 probe also imports `agentsdk.mcp`.
- **`tests/test_adr12_comparison.py`:** the packages list includes `agentsdk.mcp`.
- **`tests/test_builtin_tools.py`:** the declared-imports walk lets the `mcp` package import `mcp`,
  as `telemetry.py` imports `opentelemetry`.
- **No other approved test is changed.**

## What changed

```bash
git diff ca8783d -- agentsdk tests scripts README.md pyproject.toml requirements.txt
git status --short
```

## What the author ran

1. **Tests first.** The carried tests failed for the stated reasons before any fix. The client tests
   were written before `agentsdk.mcp` existed, and found:
   - the library's whole-listing refusal;
   - its own output validation;
   - a double connection attempt on the first turn;
   - restarts counted on failed opens;
   - the launcher escape.
2. **Mutation run: 22 of 22 killed,** each file restored by SHA-256:
   - provenance; who is sent tools; allow, deny and the name, duplicate, clash and schema skips;
   - tenancy of connections; the restart limit; timeouts; `is_error`; the default approval;
     `aclose`;
   - env values in the record; the manifest; the image size; the double attempt; the profile
     wildcard;
   - the NTFS sweep; example 23's skip; example 26's named rejection.

   One first survived, a test gap: the race's reasoned reject never happened in the test. The
   rejecting person now decides first.
3. **Example 27:** offline on both stores. Live, on the gateway and PostgreSQL, all 3 live checks
   passed, and its one run row was removed.
4. **Gates:** the first regression run failed 2 of 2212, on the two approved-test edits listed above,
   which were then made. Then both gates passed on source hash `2058f874`:
   - `unit`: 40 passed in 48.30s.
   - `regression`: 2212 passed in 490.98s (started 08:39:51 UTC). The baseline was 2172.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Probes in `m33-r1-probes` in your own scratchpad; tenant ids starting `SYN-m33r1`.

1. **Trust (NFR-35).** Can anything a server returns reach the model or the store labelled as
   anything but untrusted MCP data? Try results, errors, images, embedded resources, structured
   content, and a timeout.
2. **Who sees what (P4-D30, FR-125).** Can an agent whose profile does not name a server's tool be
   sent it, or call it? Check the `<alias>__*` wildcard against names that only look alike (another
   alias's prefix, a registered tool with `__`). Can a server's listing shadow a registered tool, or
   another server's?
3. **Tenancy (P4-D28).** Can two tenancies ever share a server process, a listing, or a tool object?
   Two loops? Is state on a connection reachable from another tenant's run?
4. **Lifecycle (FR-124, NFR-39).** Crash a server mid-call, at listing, at start, and twice. Cancel
   a run mid-call. Close the `Runner` with calls in flight. Close the event loop without `aclose`.
   Is any process left, with the real interpreter, and how bad is the launcher case? Does a failed
   connection leak a task or a file handle (`os.devnull`)?
5. **A hostile listing.** Names, duplicates, 1001 tools, pagination that never ends, huge
   descriptions, schemas with pathological patterns. Is any event loop stall reachable? Clarification
   2 puts the library's output validation on the loop.
6. **Secrets (NFR-37).** Can an `env` value reach a row, an event, the manifest, a span, a log line,
   or an exception's text, including through an error a failed start raises?
7. **The carried items (FR-122).** The NTFS comparison against `CompareStringOrdinal`'s real
   behaviour; example 26's two checks; example 23's skip.
8. **Runs without MCP (NFR-38, NFR-34).** Unchanged, and importing `agentsdk` loads no `mcp` module.
9. **The clarifications and the approved-test edits** listed above.
10. **Regressions:** the full suite, and example 27 live if you can afford it.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M32,** and the README's M33 rows.
- **The launcher escape** (clarification 5).
- **Until M34 there is no catalog:** a tool that changes is not caught.
- **HTTP, resources and prompts, and input requests** are M35 and M36.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter; latent, out-of-scope or
  cosmetic findings are caveats in your reason.
- **Clean up** every run, row, temp folder, process and lock you create, and say what you removed.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M33-mcp-stdio-client --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M33-mcp-stdio-client --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the ten attack items, and your
verdict.
