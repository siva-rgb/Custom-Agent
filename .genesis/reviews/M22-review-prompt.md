You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Fable 5.1, falling back to Sonnet 5** if Fable 5.1 fails
to start. M22 was implemented by Opus 5.5; Opus 5 approved M21a, Fable 5.1 approved M21 round 2,
and Sonnet 5 rejected M21's round 1. Bring no memory of an earlier review into this one: if your
session holds any, say so and ask for a fresh one. Re-derive the requirements from SPEC.md
("#### M22", FR-89 to FR-91, NFR-25, NFR-26, AC-71, AC-72) rather than inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.**
- **Install:** `.venv\Scripts\python.exe -m pip install -r requirements.txt`. M22 adds no library
  (NFR-24).
- **Host:** Windows Developer Mode must be on (D7).

**Only one test session at a time, and nothing else in parallel with one.** Every pytest session
compares the store's ids at its start and end (AC-44, AC-63; M22 adds `source_versions` to that
list) and the development database is shared. Check that no other python or node process is
running first. Run your own probes with `-p no:cacheprovider`: Genesis hashes .pytest_cache, and
the first run of a new test file changes it (the author's first gate run went stale that way).
The full suite takes about 5 minutes; keep every command in the foreground and under 590 s.
**`genesis gate` kills a gate after 120 s unless you pass `--timeout 590000`**, and on this host
that kills only the venv launcher (KNOWLEDGE-64fa6e18): run
`genesis gate . M22-source-versions --timeout 590000`, then check for a stray python process.
**Restore a mutated file byte for byte** and verify its SHA-256 in a `finally`. Beware `git stash`:
on this host it rewrites line endings (KNOWLEDGE-ae83f412). Several files are CRLF.

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown, so read the store from async probes with `asyncio.to_thread`.

**Concurrency probes:** at the default 5 ms thread switch interval a race can hide; set
`sys.setswitchinterval(1e-6)` while a probe contends (KNOWLEDGE-93fa7f44).

## Task under review

**M22-source-versions**, **round 1**, risk high. Phase 3 increment 1's second milestone.

### Decisions that shape it

- **P3-D5 to P3-D7** (DECISION-423f3e23) and the reviewed draft's choices (DECISION-10494890).
- **DECISION-0d384c8d** (owner, M22 pre-flight):
  1. **M21a review caveats settled inside M22** (KNOWLEDGE-bbd190e1):
     - **C1:** the README row for caveat 4 is corrected.
     - **C2 repaired:** an unbound in-memory combined write whose sink names the run is now filed
       under the sink's tenant and project. Test:
       `test_c2_an_unbound_combined_write_is_filed_with_its_sinks_tenant_not_the_first_binder`,
       added to tests/test_carried_caveats.py.
     - **C4:** AC-75 is reworded, and FR-97 is extended.

     The owner permitted re-signing the spec and plan approvals as `siva-rgb (owner)`. M22 was
     requeued and reactivated through the CLI, with no manual state edit this time.
  2. **`ledger_run_id` pulled forward from M23's FR-92**, so that SESSION scope reaches the
     top-level run as FR-90 says:
     - `RunConfig.ledger_run_id` and `ToolScope.ledger_run_id`;
     - a top-level run's ledger is its own id;
     - the SubagentPool sets every child's ledger to its parent's ledger, or else the parent's id;
     - the orchestrator passes its ledger to the pool.
  3. **Example 19** runs offline and live.

### Requirements (read them verbatim in SPEC.md)

FR-89 (the version type, migration 0010), FR-90 (the cache: key, reach, freshness, integrity,
non-2xx, copy, in-memory store), FR-91 (the built-in fetch records and serves; FR-38's final-URL
source superseded inside a run; configuration and `schema_hash`; events; example 19), NFR-25,
NFR-26, AC-71, AC-72.

## What changed

**Nothing since M21a is committed.** HEAD is `fa64753` (M21a). Review with:

```bash
git diff fa64753 -- agentsdk tests scripts README.md SPEC.md
git status --short
```

- `agentsdk/evidence.py` (new). It holds:
  - `CacheScope`;
  - `EvidenceSourceVersion` (frozen, every field validated by name; PUBLIC_GLOBAL refused with an
    `auth_scope_hash`);
  - `ResourceCacheKey` and `canonical_uri`;
  - `reaches()`, the reach rule;
  - the `EvidenceStore` protocol and `InMemoryEvidenceStore`;
  - `ResourceCache`, one run's view, which serves (own fresh versions first, then a copy of a
    foreign one), records, works out the prior version, and emits events.
- `agentsdk/migrations/0010_source_versions.sql` (new). Points to check:
  - `artifact_id` is deliberately not a foreign key, so that a missing artifact is a miss;
  - `source_run` references `runs`;
  - `ledger_run_id` and `request_variant` are store columns that are not on the public type;
  - `recorded_at` orders ties.
- `agentsdk/postgres.py`: `PostgresEvidenceStore` (`record`, `get`, `reachable`). `reachable` is
  the one statement that reads another tenant's rows, and only PUBLIC_GLOBAL or same-tenant
  TENANT rows.
- `agentsdk/builtin_tools.py`:
  - `fetch_tool` gains `cache_scope`, `host_scopes`, `freshness_seconds` and a `_clock` seam;
  - `fetch_url` takes the injected `ToolScope`;
  - `_fetch_recorded` checks the allowlist before the cache;
  - the non-default cache options enter `configuration` only when set.
- `agentsdk/api.py`:
  - `RunConfig.ledger_run_id`;
  - `Runner._evidence_store()`;
  - each run's `ToolScope` carries `ledger_run_id` and a `ResourceCache`, whose store calls go
    through `control.store`.
- `agentsdk/scope.py`, `subagents.py`, `orchestrator.py`, `persistence.py`
  (`evidence_store()`), `events.py` (`SourceFetched`, `SourceServed`), `__init__.py` (exports).
- `agentsdk/session.py`: the C2 repair (above).
- `scripts/19_sources.py` (new). `README.md`: a feature bullet, example 19's row, three limit
  rows (two M22, one corrected).
- `SPEC.md`: AC-75 and FR-97 (C2, C4), and FR-90/FR-92 (ledger_run_id in M22).
- `tests/test_sources.py` (new, the gate file, 121 tests); `tests/conftest.py` tracks
  `source_versions` for AC-44.
- **Edits to approved tests, for review:**
  - `tests/test_builtin_tools.py` (`test_fetch_and_search_results_carry_exactly_fr40_labels_into_the_assembler`):
    inside a Runner the fetch's source is now its version's uri, and the final URL is asserted
    on the version, as FR-91 requires.
    - The other three lines FR-91 names (391, about 1782 and about 2117) call the tool outside
      a Runner, so they keep FR-38's final URL, as FR-91 also says, and are unchanged.
  - `tests/test_distribution.py`: `19_sources.py` added to the expected examples (FR-56).
  - `tests/test_carried_caveats.py`: one test added (C2).

### Choices the spec left open, made by the author (judge them)

- **The version's content is the fetch tool's whole text result**, `[HTTP 200]` status line and
  any truncation note included. `content_hash` is over that text, so a served page reads exactly
  as a fresh fetch returns it (FR-91). The artifact's mime type is `text/plain`; the version's
  `media_type` is the response's.
- **`final_uri` is the URL as fetched**, as FR-38 named it (port and fragment as given), while
  `canonical_uri` is canonical.
- **A copy keeps the original's `final_uri` and `media_type`** along with its `content_hash` and
  `retrieval_time`. It takes the reader's tool scope for that host, and its `prior_version` is the
  reader's own newest version for the key, or None. It names none of the original's tenant,
  project, run, artifact or id. Declared in the README.
- **The prior version** of a new version is the newest version within reach that is in the
  reader's own tenant and project (stale or unreadable). A foreign version is never named.
- **A NO_CACHE fetch never looks the cache up**, and records a version with no prior.
- **The served event's `age_seconds`** is measured from the version's `retrieval_time` (for a copy,
  the original's).
- **The allowlist is checked before the cache**, so a cached page is never served for a URL this
  tool refuses.

## What the author ran

1. **The gate file:** 121 tests, written before the implementation and run to green.
   - Two tests were corrected on the way: the agent's tool profile, and `final_uri` as fetched.
   - Four were added before the mutation run: the allowlist before the cache, NO_CACHE bypassing
     it, own-first, and a version whose own hash no longer matches.
2. **Mutation run: 24 of 24 killed**, each file restored by SHA-256.
   - One survivor in the first run: a copy taking a fresh retrieval time. The scope-table test
     never moved the clock; it now moves it 5 s before the reader fetches.
   - The mutants covered:
     - each reach rule, in memory and in SQL;
     - freshness at its boundary;
     - share instead of copy, and own-first;
     - the version's own hash, the prior, the copy's prior and time;
     - the PUBLIC_GLOBAL check and the default port;
     - non-2xx recorded, the allowlist after the cache, and NO_CACHE lookup;
     - keying by the final URI, `host_scopes`, and the configuration;
     - `get` and tenancy;
     - the pool's and Runner's ledger.
3. **Gates.** Both green on source hash `eeb7c464`, from one `genesis gate --timeout 590000` run.
   - `unit`: `tests/test_sources.py`, exit 0, 121 passed in 12.46s.
   - `regression`: the full suite, exit 0, 1797 passed in 300.58s (14:07:30 to 14:12:32 UTC). The
     baseline before M22 was 1675.
   - An earlier gate run went stale between unit and regression, because the first run of the new
     file changed .pytest_cache. Re-running settled it.
4. **Example 19.**
   - Offline: 7 of 7.
   - Live, against example.com, the gateway and Postgres: 6 of 6 on the second attempt. In the
     first attempt the model fetched once per run, not twice, and three strict checks failed; the
     instructions were tightened.
   - Each live run's rows were removed by run id. The store is at baseline: example-tenant has 19
     runs, there are 6 artifacts and 0 source versions, and nothing is running.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m22-r1-probes` in your own session's scratchpad, with tenant ids
starting `SYN-m22r1`; delete only rows your probes wrote, by the run ids they recorded.

1. **Tenancy (NFR-25).**
   - Can any reader be served, or shown, a version, artifact or identifier of another tenant or
     project, outside the copy?
   - Does the copy carry anything of the original beyond what is declared?
   - Read `reachable`'s SQL and `reaches()` side by side.
   - Can SESSION leak across top-level runs, or across tenants that share a ledger id?
2. **Integrity (NFR-26).**
   - Any path that serves content not matching the version's `content_hash`.
   - Any method or SQL that updates or deletes a version.
   - A version recorded without its artifact, or the reverse, under cancellation or a store
     failure: is either reachable, and does it matter?
3. **Freshness and keys.**
   - The boundary; clock skew (a `retrieval_time` in the future).
   - Canonicalisation edge cases: IDN, IPv6, a trailing dot, an empty path versus `/`, a query
     with an empty value.
   - Do two URLs that `_target` treats as the same host get different keys, and does that matter?
4. **The fetch path.**
   - Non-2xx, redirects, refusals and the deadline, inside and outside a Runner.
   - Is FR-38's behaviour outside a Runner byte-for-byte unchanged?
   - Is the default `schema_hash` truly unchanged, against HEAD?
5. **The ledger run id.** Every path that builds a run:
   - a top-level run;
   - a subagent;
   - a grandchild;
   - an orchestrator child;
   - a replan;
   - a criterion executor's scope (orchestrator.py, about line 486, which builds a ToolScope with
     no evidence).

   Does anything build a scope with the wrong ledger, or reach the cache without one?
6. **Concurrency and FR-81.** Concurrent fetches of one key in one run and across runs, both
   stores, at a 1 microsecond switch interval.
   - Is every store call on a worker thread?
   - Does anything raise or corrupt, beyond the declared "concurrent misses each fetch until
     M24"?
7. **The C2 repair and the re-signing.**
   - Is the session.py change sound?
   - Is the spec text faithful?
   - Did the re-approval through the CLI leave the task list and the M21/M21a records untouched?
     Check with `git diff fa64753 -- .genesis/project.json`.
8. **Regressions and the approved-test edits** listed above.

## Declared limitations: known, recorded, NOT findings

- **Until M24, concurrent misses on one key may each fetch, and each is recorded** (FR-90).
- **A source version's content is the tool's whole result, and a copy keeps the original's
  `final_uri` and `media_type`** (README, Declared M22).
- **A version can outlive its artifact** (an artifact is deletable); such a version is a miss and
  is refetched (FR-90).
- **The SDK's own fetch sends no credential.** `auth_scope_hash` and `request_variant` are always
  None for it, and are exercised only through the store.
- Everything declared for M20 to M21a still stands (KNOWLEDGE-cc4d686c, KNOWLEDGE-afac039f,
  KNOWLEDGE-8e29cfe3, KNOWLEDGE-bbd190e1), except C1, C2 and C4 of KNOWLEDGE-bbd190e1, which this
  settles.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. Latent, out-of-scope or
  cosmetic findings are caveats in your reason, not blockers.
- **Restore every file you mutate** and verify SHA-256, restoring in a `finally`.
- **Clean up** every run, row, temp folder and process you create, and say what you removed.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M22-source-versions --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M22-source-versions --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the eight attack items, and
your verdict.
