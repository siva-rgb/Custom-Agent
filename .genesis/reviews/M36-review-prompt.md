You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Sonnet 5, falling back to Fable 5.1** if Sonnet 5 fails to
start. M36 was implemented by Opus 5.5; its pre-review was run by Opus 5, and M35 was reviewed by
Fable 5.1. Bring no memory of an earlier review into this one: if your session holds any, say so
and ask for a fresh one.

Re-derive the requirements from SPEC.md:
- "#### M36" (FR-133, FR-134), AC-93;
- NFR-30, NFR-31, NFR-35, NFR-37, NFR-38 (amended after M35: the edits an increment needs, each
  named here with its reason and checked by content);
- the P4-D19 row.

Read the records:
- KNOWLEDGE-dc116498: M35 approved, and the items it carried to M36;
- KNOWLEDGE-fe6eb6d4: M36's pre-flight: mcp 2.3.0 handles input_required itself;
- DECISION-d4a40cfe: the owner's rule for a legacy-era request that names no call;
- KNOWLEDGE-35270cef: attribution over HTTP, and the cancellation and clock findings;
- KNOWLEDGE-d86bd11c: M36's pre-review, six defects found and fixed.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-4`; HEAD is `7f52a13` (M35). M36 is uncommitted. **Migration `0018` is applied** to the
development database and is never edited.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs:** the gate writes `.genesis/project.json` back when it
ends, and a record made meanwhile is lost.

- **Configuration:** DATABASE_URL, BASE_URL and MODEL_API_KEY from .env. **Never print credentials
  or a connection string.**
- **Host:** Windows, Python 3.11, `mcp==2.3.0`, `httpx2==2.12.0`. **This laptop enters Modern
  Standby on battery when idle,** which suspends a gate mid-run; run long commands under a wrapper
  holding `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`.

**Running tests**
- **One test session at a time.** `tests/conftest.py` refuses a second one with exit code 3.
- Check that no other python or node process is running first. Run probes with
  `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **Prefer runtime mutation,** leaving no repository file changed. Restore any edited file byte
  for byte, verified by SHA-256. **Check that no repository file changed by content, never by
  mtime.**
- **MCP tests start real server processes,** stdio and local HTTP. After every run, check for a
  leftover `mcp_servers.py`, `mcp_http_servers.py`, `time.sleep(600)` or `addsitedir` process.
  Remove only those your own run started.
- **Timings:** the full suite takes about 14 to 18 minutes; `tests/test_mcp_input.py` about 8.

**Gates:** `genesis gate . M36-mcp-input --timeout 1500000` in the background.

**Please complete every attack item, and verify the gate evidence yourself.**

**Cleanup order:** `interruption_decisions`, then `interruptions` and `standing_decisions`, then the
run's other rows, then the runs, children first, by run id. Catalog rows carry a tenancy, not a run:
remove only those of tenancies your probes created (tenant ids starting `SYN-m36r1`).

## Task under review

**M36-mcp-input**, round 1, risk **high**.

### What M36 builds

**1. The record (FR-133, migration `0018`).** An input request is an interruption of kind
`additional_user_input` (an elicitation, form or url) or `external_tool_input` (sampling, roots),
trigger `server`, answered `accept`, `decline` or `cancel`; expiry and the run's cancellation
answer `cancel`; a request the client does not support is declined with source `unsupported`. An
approval is never answered, nor an input request decided: `InterruptionKindMismatch`, told second
after tenancy. Migration `0018` replaces `0014`'s checks by name and adds a trigger that refuses a
decision of the wrong kind at the database. An accepted form's content is stored as `response`,
with its labels (`RESPONSE_PROVENANCE`: user origin, data only, untrusted, `user_controlled`).

**2. Answering (`Runner.respond`, `RunHandle.respond`).** Refused in `decide`'s order with the kind
second. Content: required for `accept` in form mode, checked against the server's schema in an
isolated process, then against the protocol's `ElicitResult` content and a 64 KiB bound
(pre-review D3); none for any other answer.

**3. The wait (`agentsdk/mcp/__init__.py`).** mcp 2.3.0 drives 2026-07-28's input_required retry
itself and hands every request to one elicitation callback, the legacy era's too, so the SDK builds
no retry (KNOWLEDGE-fe6eb6d4). Each MCP call carries an `_AskingCall` (its run's scope, its
approval, its request's context) in a context variable:
- **attribution:** the request's own context on 2026-07-28, and on the legacy era when it comes on
  the call's response stream over HTTP; otherwise one call in flight, else `cancel` and
  `InputUnattributed` to each (the owner, DECISION-d4a40cfe); an application read's request is
  answered `cancel`, no run told (pre-review D5);
- **waiting:** recorded, registered with `ApprovalWaits`, re-read on each wake, at least every five
  seconds and at expiry, as an approval is (FR-115); neither the library's request clock (paused
  through its private `_request_clock`, mcp 2.3.0 pinned) nor the SDK's own deadline (`_within`)
  runs while any request of the call waits (pre-review D1); the caller's bound includes three waits
  of `approval_timeout` (pre-review D2);
- **cancellation:** the run's cancellation decides `cancel`, written by a task of its own, since
  the legacy era's wait runs in the library's anyio task group;
- **a fourth request** fails the call; sampling and roots count toward it (pre-review D4);
- **sampling and roots:** recorded by wrapping the session's `dispatch_input_request`; neither
  capability is ever declared; the library's default answers them unsupported.

**4. Example 30** (`scripts/30_mcp_input.py`): a form accepted by a scripted person, a URL
declined and never opened; offline on both stores and live.

**5. Carried from M35:** configured headers join each request in the transport, never the HTTP
client's own headers; `Accept-Encoding` cannot be configured; content-type parameters kept,
quoted ones too.

### Every carried finding, answered

This table is the contract. **If any item carried or found is missing from it, or its status is
false, that alone is a finding.**

| From | Finding | Status |
|---|---|---|
| M35 approval, 1 | httpx2 masks only Authorization in `repr(client.headers)` | Fixed: the headers live in the transport (`test_no_configured_header_value_is_in_the_http_clients_repr`) |
| M35 approval, 2 | `Accept-Encoding` could be configured | Fixed (`test_accept_encoding_cannot_be_configured`) |
| M35 approval, 3 | Content-type parameters dropped | Fixed, quoted ones too (`test_a_resources_content_type_keeps_its_parameters`) |
| M35 approval, 4 | A change notified before the listen stream opens is caught only by the 60 s bound | Declared in the README: one extra read per modern session to close a window the 60 s bound covers |
| M34, still open | Two of six M34 pre-flight live runs of example 27 failed before any tool ran | Still open, undiagnosed; examples 27 to 30 run live below |
| M36 pre-flight | Does mcp 2.3.0 handle input_required itself (FR-133's question) | Yes: one callback serves both eras (KNOWLEDGE-fe6eb6d4) |
| M36 pre-flight | A legacy-era request names no call | Owner's rule (DECISION-d4a40cfe); found exact over HTTP on the call's own stream (KNOWLEDGE-35270cef), narrowed by C1 below |
| M36 build | The legacy wait's cancel was never written (anyio's level-triggered cancellation) | Fixed: a task of its own (`test_cancelling_the_run_decides_the_request_cancel`) |
| M36 mutation | `decide` on an answered input request reported "already decided", not the kind | Test added; mutant killed |
| M36 full suite | M29's `test_only_a_tool_approval_is_recorded` asserted no other kind is ever recorded | Edited: other kinds still refused, an input kind under an approval's trigger refused |
| M36 full suite | The pre-review fix put `response_provenance` inside `__post_init__`, so `message` and `decided_at` went unchecked | Fixed; caught by M29's `test_a_decision_names_who_or_what_made_it` |
| M36 pre-review D1 | Two waits at once: the first answer set the call's deadline running | Fixed (`test_requests_asked_together_...`) |
| M36 pre-review D2 | Every wait cut at `_call_bound`, far short of `approval_timeout` | Fixed (`test_a_wait_lasts_its_approval_timeout_...`) |
| M36 pre-review D3 | An answer `ElicitResult` cannot carry was recorded accept and never delivered | Fixed (`test_an_answer_the_server_could_not_be_given_...`) |
| M36 pre-review D4 | Sampling and roots uncapped, a row each | Fixed (`test_sampling_and_roots_count_toward_...`) |
| M36 pre-review D5 | An application read's request told an unrelated run `InputUnattributed` | Fixed (`test_an_application_reads_request_...`) |
| M36 pre-review D6 | A `read_resource` call's request cancelled with no record | Fixed (`test_a_read_resource_calls_request_...`) |
| M36 pre-review C1 | Legacy request on the HTTP standalone stream: not exact | README corrected; the one-in-flight rule applies |
| M36 pre-review C2, C4 | Library SSE noise on cancel; how a fourth request ends per era | Declared in the README |
| M36 pre-review C3 | No size bound on an answer | Fixed with D3: 64 KiB |
| M36 pre-review C5 | `RESPONSE_PROVENANCE` stored nowhere | Fixed (`test_an_accepted_answer_is_stored_with_its_labels`) |
| M36 pre-review C6 | Quoted content-type parameters dropped | Fixed |

### How the author checked it

- **Tests first:** `tests/test_mcp_input.py`, written before the code; now 69 tests, on both stores
  and both eras, over HTTP (M35's fixture) and stdio (M33's legacy fixture).
- **Mutation:** 29 of 29 killed by named assertions, each by its own test: 21 on the build (one
  survivor, then a test added; two kills by hang, then made assertion kills), 8 on the pre-review
  fixes.
- **A fresh pre-review agent** (Opus 5, runtime probes only, KNOWLEDGE-d86bd11c) found D1 to D6;
  each was reproduced first-hand, fixed and pinned. Its probes are in the author's scratchpad,
  `m36/prereview`.
- **Gates,** on source hash `e77a34f8`:
  - `unit`: 69 passed in 180.42s;
  - `regression`: 2510 passed in 890.94s (started 07:55:41 UTC on 2026-10-22), on the first run.
- **Live:** after the gates, example 30 passed 5 of 5, example 29 passed 6 of 6, example 28 passed 6 of 6, and example 27 passed 3 of 3, against the live model; example 30's run carried two InputRequested and two InputAnswered events. Each run's status and events were read before its rows were removed by their recorded ids, children first; the catalog rows by tenancy and alias, counted first; and example 29's one resource artifact by its tenancy. The store holds 10351 runs (19 under example-tenant), 0 SYN, empty catalog tables, 6 artifacts and 0 interruptions, as before.

### Edits to approved tests

Each named with its reason (NFR-38, amended after M35); **check each by content.**

- **`tests/test_mcp_client.py`** (M33's): the two invariant tables gain M36's waits, calls, loops and
  imports, each with its bound; entries of code that changed (`_call`'s deadline, `_request`, the
  tool functions' bounds) follow it.
- **`tests/test_approvals.py`** (M30's): `test_only_a_tool_approval_is_recorded` -- FR-133 records
  input kinds; the test now asserts the other kinds are refused and an input kind under an
  approval's trigger is refused.
- **`tests/test_mcp_http.py`** (M35's): one assertion now expects the configured header absent from
  the client's own headers (M35 review).
- **`tests/test_distribution.py`:** example 30 in `EXPECTED_EXAMPLES`.
- **`tests/fixtures/mcp_servers.py`** (M33's legacy fixture): an `ask` tool sending
  `elicitation/create`, holding requests read while it waits and serving them after.
- **`tests/fixtures/mcp_http_servers.py`** (M35's): tools `ask` (form or url, `times`, `together`,
  `schema`, `delay`), `sample`, `roots`, `slow`; a resource `ask://form` that asks.
- `tests/conftest.py`: unedited -- migration `0018` adds no table.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Probes in `m36-r1-probes` in your own scratchpad; tenant ids starting `SYN-m36r1`.

0. **The table above:** check every row is true, and every approved-test edit by content.
1. **NFR-30, NFR-31:** can an answer reach the wrong call, run or tenancy; be given twice, after
   expiry, or by an agent-only principal; can `respond` answer an approval or `decide` an input
   request? Migration `0018`'s constraints and trigger, on PostgreSQL.
2. **Attribution:** concurrent runs over HTTP (both eras) and stdio (legacy); application reads;
   `read_resource`; a request on the legacy standalone stream.
3. **The wait:** past `call_timeout` (both deadlines), to `approval_timeout`; several requests at
   once; cancellation and `aclose()` mid-wait; no task, thread or process left.
4. **What reaches the server:** each answer exactly once, as itself; hostile schemas; content the
   protocol cannot carry.
5. **What is recorded and emitted:** the request (message, schema, full URL, approved_by), the
   events (never the response), the stored response and its labels; no URL ever opened.
6. **Sampling and roots:** recorded, declined, capped, never declared.
7. **The carried M35 fixes.**
8. **Regressions:** approvals (M30 to M32, orchestration included), the full suite, and examples 27
   to 30 live.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M35,** and the README's M33 to M36 rows.
- **Owner's choices:** DECISION-d4a40cfe's attribution rule; the pins of mcp and httpx2, and the
  private request clock of mcp 2.3.0.
- **SPEC's declared:** a server may ask for a secret in form mode; a url may carry a token, stored
  whole.

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
  control approve . M36-mcp-input --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M36-mcp-input --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the nine attack items, and
your verdict.
