You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Fable 5.1, falling back to Opus 5** if Fable 5.1 fails to
start. M35 was implemented by Opus 5.5; its pre-review was run by Sonnet 5, and M34's last round
was reviewed by Sonnet 5. Bring no memory of an earlier review into this one: if your session
holds any, say so and ask for a fresh one.

Re-derive the requirements from SPEC.md:
- "#### M35" (FR-130 to FR-132), AC-92;
- FR-127's amended hash and read rules, AC-91's amended change clause;
- NFR-34 (amended: the `httpx2` pin), NFR-35, NFR-36, NFR-37, NFR-38;
- the P4-D18, P4-D19 and P4-D23 rows.

Read the records:
- KNOWLEDGE-5fe1114f: M34 approved, and the five items it carried to M35;
- KNOWLEDGE-9793a03b: M35's pre-flight: the baseline, and what mcp 2.3.0 gives for HTTP;
- DECISION-57102525: the owner's four pre-flight choices (the hash, timed-out verdicts, the
  4 MiB cap, FR-127's wording);
- DECISION-2129074c: the owner's three design choices (no URL query, templates gate listing
  only, the SDK's read_resource wins) and the one transport;
- DECISION-cae98801: the `httpx2==2.12.0` pin (NFR-34 amended);
- KNOWLEDGE-2ac86f16: M35's pre-review, four defects found and fixed.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-4`; HEAD is `3772dbc` (M34). M35 is uncommitted.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs:** the gate writes `.genesis/project.json` back when it
ends, and a record made meanwhile is lost.

- **Configuration:** DATABASE_URL, BASE_URL and MODEL_API_KEY from .env. **Never print credentials
  or a connection string.** Strip the quotes if you export .env by hand.
- **Host:** Windows, Python 3.11, `mcp==2.3.0` and `httpx2==2.12.0` in the venv. **This laptop
  enters Modern Standby on battery when idle,** which suspends a gate mid-run; run long commands
  under a wrapper holding `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`.

**Running tests**
- **One test session at a time.** `tests/conftest.py` refuses a second one with exit code 3.
- Check that no other python or node process is running first. Run probes with
  `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **Prefer runtime mutation,** leaving no repository file changed. Restore any edited file byte
  for byte, verified by SHA-256, and never touch an applied migration (`0014` to `0017`). **Check
  that no repository file changed by content (SHA-256 or `git diff`), never by mtime.**
- **MCP tests start real server processes,** stdio and local HTTP ones, some of which never answer
  on purpose. After every run, check for a leftover `mcp_servers.py`, `mcp_http_servers.py`,
  `time.sleep(600)` or `addsitedir` process. Remove only those your own run started.
- **Expensive probes go in a child process,** started with `sys._base_executable`.
- **Timings:** the full suite takes about 12 to 16 minutes; `tests/test_mcp_http.py` about 2.

**Gates:** `genesis gate . M35-mcp-http-resources --timeout 1500000` in the background.

**Please complete every attack item, and verify the gate evidence yourself.**

**Cleanup order:** `interruption_decisions`, then `interruptions` and `standing_decisions`, then the
run's other rows, then the runs, children first, by run id. Catalog rows and resource artifacts
carry a tenancy, not a run: remove only those of tenancies your probes created (tenant ids
starting `SYN-m35r1`).

## Task under review

**M35-mcp-http-resources**, round 1, risk **high**.

### What M35 builds

**1. `HttpServer` (FR-130).**
- **The URL:** `https`, or `http` to an IP loopback literal or `localhost`. No user information,
  no query (the owner: a credential belongs in headers) and no fragment, refused when built.
- **Headers:** names are HTTP tokens; values are visible ASCII with spaces or tabs only inside
  (pre-review D1); the headers the SDK or the library sets are refused. Values never reach a
  repr, record, event, row, log line or exception's text (NFR-37). The record names them only.
- **The common fields** are StdioServer's, checked by one function both share
  (`_check_common`), plus `resources_as_tool` on both.

**2. One transport (DECISION-2129074c).** Every request of an HTTP connection goes through one
httpx2 client the SDK builds (`_http_client`), whose transport sees each response before the
library does:
- a 3xx is refused, `max_redirects=0` as well, and no environment proxy is used;
- a 401 or 403 becomes `refused authorization`;
- a body over 4 MiB is refused as it arrives, before it is parsed; an event stream is ended past
  4 MiB; the library's own 1 MiB event limit fails first on the legacy era's SSE;
- an encoded response is refused, and identity is asked for (pre-review D2).

Each refusal is answered to the library as a JSON-RPC error for that request id, carrying a
per-connection nonce, so the connection stays usable and no server can pass its own error off as
one. At connection a refusal is named `RefusedAuthorization`, `RedirectRefused`,
`ResponseTooLarge` or `EncodingRefused`. Over HTTP a `-32000` fails only that call. The client is
closed with its session, since the library leaves a client it is given open.

**3. Resources and prompts (FR-131).**
- `Runner.mcp_resources` returns the resources, the templates accepted in their current version,
  and the blocked ones; `mcp_read` stores a read as an artifact of the tenancy with NFR-35's
  labels, `created_by_agent="mcp:<alias>"`, and returns its `ArtifactRef`. A template's
  acceptance gates its listing only (the owner).
- `Runner.mcp_prompts` and `mcp_prompt` return an `McpPrompt` only for a prompt accepted in its
  current version; otherwise `ToolBlocked`.
- Lists are read again at every call; their versions carry their kind, and a change is told only
  from a version of the same kind (`last_accepted(..., kind)` on both stores).
- `resources_as_tool=True` offers `<alias>__read_resource(uri)`, the SDK's tool: it wins over a
  server tool of that name, follows `approval_overrides["read_resource"]` else the server's
  approval, and needs no acceptance (the owner).

**4. The hash and the carried items (DECISION-57102525).** Hashes cover every field a person or
a model can read, title included, never `_meta`, `icons` or `execution`. A timed-out schema check
is refused and kept for the connection's life. A listing's age runs from the start of its read.
A 2026-07-28 server that does not offer listen is not asked again that session; any other failure
to open it is a lost stream, reopened at the next read (pre-review D4).

**5. Example 29** (`scripts/29_mcp_http.py`, FR-132) runs a local HTTP server in the same file,
offline on both stores and live.

### Every carried finding, answered

This table is the contract. **If any item that M34's approval carried, or M35's pre-flight or
pre-review found, is missing from it, or its status is false, that alone is a finding.**

| From | Finding | Status |
|---|---|---|
| M34 approval, item 1 | A schema check that timed out was never cached, so with `ttlMs` 0 it was re-checked at every read | Fixed (owner): refused and kept for the connection's life (`test_a_schema_whose_check_timed_out_is_checked_once_per_connection`) |
| M34 approval, item 2 | `_listed_at` was stamped at the end of a read | Fixed: aged from its start (`test_a_listings_age_runs_from_the_start_of_its_read`) |
| M34 approval, item 3 | A modern server not offering listen may make every listing stale | Measured at pre-flight (5 reads in 5 runs, against 1 with listen); fixed (`test_a_modern_server_without_listen_is_not_read_at_every_turn`) |
| M34 approval, item 4 | SPEC said re-read before "a turn's first call"; the code reads more often | Fixed: FR-127 reworded to name every read; spec re-approved |
| M34 approval, item 5 | title, `_meta`, icons, `execution` and unknown annotation keys outside the hash | Decided (owner): title hashed, the rest not, declared; the library drops unknown annotation keys (`test_a_tool_change_blocks_by_what_a_reader_sees`) |
| M34, still open | Two of six pre-flight live runs of example 27 failed before any tool ran | Still open, undiagnosed: not reproduced since; examples 27 to 29 run live below |
| M34 round 3 | "approved tests unedited" was checked by mtime | Process: checked by content here; asked of you above |
| M35 pre-flight | mcp 2.3.0 reads a JSON body whole, with no limit | Fixed (owner): the 4 MiB cap, counted before parsing |
| M35 pre-flight | 401, 403 and 500 reach the client as one error; same-origin redirects are followed | Fixed: the transport refuses them itself |
| M35 full suite | `agentsdk/mcp` imported `httpx2`, which the SDK did not declare | Fixed (owner): pinned in the extra, NFR-34 amended, spec re-approved |
| M35 pre-review D1 | A header value h11 refuses was quoted whole in a DEBUG log | Fixed: values must be visible ASCII (`test_a_header_that_cannot_be_sent_safely_is_refused_when_built`) |
| M35 pre-review D2 | A gzip body passed the 4 MiB cap encoded and was inflated after | Fixed: identity asked for, any encoding refused (`test_an_encoded_response_...`) |
| M35 pre-review D3 | An unstorable `mcp_read` uri raised a bare error, or reached the server | Fixed: refused before it is sent (`test_a_uri_that_cannot_be_stored_...`) |
| M35 pre-review D4 | A transient failure opening listen was taken as "not offered" for the session | Fixed: only -32601 or `ListenNotSupportedError` means not offered (`test_a_listen_that_fails_to_open_...`) |
| M35 pre-review C1 to C5 | Case-sensitive content type; read_resource not filtered by allow/deny; a shared version limit; path credentials recorded; the 4 MiB cap on a long listen stream | C1 fixed; C2 to C5 declared in the README |

### How the author checked it

- **Tests first:** `tests/test_mcp_http.py`, written before the code; now 106 tests, on both
  stores and both eras, against a real local HTTP server (`tests/fixtures/mcp_http_servers.py`,
  new). It serves the legacy era with `--legacy-only`, refusing `server/discover`, so `auto` must
  fall back.
- **Mutation:** 30 of 30 killed by named assertions, each by its own test. Two first kills were
  invalid (an SQL error, a syntax error) and were rewritten; the kind filter's mutants survived
  at first, because a cached blocked event hid the store, and a store-level test now kills them.
- **A fresh pre-review agent** (Sonnet 5, runtime probes only, KNOWLEDGE-2ac86f16) found D1 to D4;
  each was reproduced first-hand, fixed and pinned. Its probes are in the author's scratchpad,
  `m35/prereview`, and you may rerun them.
- **Gates,** on source hash `e511c0d2`:
  - `unit`: 106 passed in 86.05s;
  - `regression`: 2441 passed in 709.07s (started 18:32:44 UTC on 2026-10-21). The first run failed only `test_golden_eval.py`'s live Bedrock Haiku test, on a provider outage (`ModelProviderUnavailable`, as in M33 and M34); the second landed on alternating hashes, which the third cleared.
- **Live:** after the gates, example 29 passed 6 of 6, example 28 passed 6 of 6, and example 27 passed 3 of 3, against the live model. Each run's status and events were read before its rows were removed by their recorded ids; the catalog rows by tenancy and alias, counted first; and example 29's one resource artifact by its tenancy, counted first. The store holds 10351 runs (19 under example-tenant), 0 SYN, 0 running, empty catalog tables and 6 artifacts, as before.

### Edits to approved tests

**NFR-38 names the edits it permits:** those FR-122 and FR-123 name; `tests/test_distribution.py`'s
exports and `EXPECTED_EXAMPLES`; and `tests/conftest.py`'s tracked tables. M35 adds no table, so
`conftest.py` is unedited. **Every edit below is listed; check each by content.**

- **`tests/test_distribution.py`:** example 29 in `EXPECTED_EXAMPLES`; the `mcp` extra's pin
  assertion gains `httpx2==2.12.0`; the import probe checks that no `httpx2` module loads either
  (the owner's pin, DECISION-cae98801).
- **`tests/test_builtin_tools.py`:** the import allowlist for `agentsdk/mcp` gains `httpx2`
  (outside NFR-38's list; the owner's pin).
- **`tests/test_mcp_client.py`** (M33's): the two invariant tables gain the new waits, calls and
  loops, each with its bound; the entries of code that moved (the shared config checks, the
  wait's `while`) move with it; `StdioServer`'s `record` keeps only its own fields.
- **`tests/test_mcp_catalog.py`** (M34's): `PARTS` gains `title` (AC-91 amended); the read-back
  entry gains `"title": None`.
- **`tests/fixtures/mcp_servers.py`** (M33's fixture): `"listen": false` in the state file serves
  a modern catalog server with no `subscriptions/listen`.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Probes in `m35-r1-probes` in your own scratchpad; tenant ids starting `SYN-m35r1`.

0. **The table above:** check every row is true, and every approved-test edit by content.
1. **NFR-37:** can a header value reach any repr, record, event, row, span, log line (all
   loggers, DEBUG) or exception text, at connection, mid-call, or on refusal?
2. **The transport:** can any response reach the library without passing the guard -- a status,
   an encoding, a stream, a body size, a redirect -- and is any URL from a response ever opened?
3. **The URL and header rules:** a non-loopback http host, a credential, a header injection.
4. **NFR-36 for prompts and templates:** a prompt in a version not accepted for its tenancy,
   across tenancies, or confused with a tool or template of its name; unbounded listing cost.
5. **`<alias>__read_resource`:** offered only with `resources_as_tool` and to a profile naming
   it; approval; shadowing; untrusted results.
6. **`mcp_read`:** labels, tenancy, content of each kind, a hostile mime type or uri; and the child
   briefed with it (FR-83).
7. **The carried items and the listen logic:** can a real change notification be missed, or a lost
   stream never reopened?
8. **Regressions:** the full suite, and examples 27, 28 and 29 live.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M34,** and the README's M33, M34 and M35 rows.
- **The bounds working:** a refusal, an over-size body or an encoded one fails its call or leaves
  the server unavailable, as stated.
- **Owner's choices:** a URL query refused; templates gate listing only; the SDK's read_resource
  wins; title hashed and `_meta`, icons and `execution` not; the `httpx2` pin.

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
  control approve . M35-mcp-http-resources --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M35-mcp-http-resources --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the nine attack items, and
your verdict.
