You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Sonnet 5, falling back to Opus 5** if Sonnet 5 fails to
start. M23 was implemented by Opus 5.5. Earlier reviews: Fable 5.1 approved M22, Opus 5 approved
M21a, and Fable 5.1 approved M21 round 2. Bring no memory of an earlier review into this one: if
your session holds any, say so and ask for a fresh one. Re-derive the requirements from SPEC.md
("#### M23", FR-92 to FR-94, NFR-25, NFR-26, AC-73, and the M22 caveat sentences in FR-89, FR-90
and FR-92) rather than inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.**
- **Install:** `.venv\Scripts\python.exe -m pip install -r requirements.txt`. M23 adds no library
  (NFR-24).
- **Host:** Windows Developer Mode must be on (D7).

**Only one test session at a time, and nothing else in parallel with one.**
- Every pytest session compares the store's ids at its start and end (AC-44). M23 adds `claims`
  and `claim_contributions` to that comparison, and the development database is shared.
- Check that no other python or node process is running first.
- Run your own probes with `-p no:cacheprovider`: Genesis hashes .pytest_cache.
- The full suite takes about 6 minutes; keep every command in the foreground and under 590 s.

**Gates:**
- **`genesis gate` kills a gate after 120 s unless you pass `--timeout 590000`.** On this host
  that kills only the venv launcher (KNOWLEDGE-64fa6e18).
- Run `genesis gate . M23-evidence-ledger --timeout 590000`, then check for a stray python
  process.

**Mutations:**
- **Restore a mutated file byte for byte** and verify its SHA-256 in a `finally`.
- Beware `git stash`: on this host it rewrites line endings (KNOWLEDGE-ae83f412). Several files
  are CRLF.

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown, so read the store from async probes with `asyncio.to_thread`;
the `EvidenceLedger` reader's methods are store calls too.

**Concurrency probes:** at the default 5 ms thread switch interval a race can hide; set
`sys.setswitchinterval(1e-6)` while a probe contends (KNOWLEDGE-93fa7f44).

## Task under review

**M23-evidence-ledger**, **round 1**, risk high. Phase 3 increment 1's third milestone.

### Decisions that shape it

- **P3-D2, P3-D8, P3-D9, P3-D11** (DECISION-39f28e48, DECISION-423f3e23), and the reviewed draft's
  choices (DECISION-10494890).
- **DECISION-9de80b77** (owner, M23 pre-flight):
  - **Repaired:** M22's review caveats 1 to 5 (KNOWLEDGE-2763f57b):
    1. the criterion executor's scope now carries the run's ledger and cache;
    2. a test runs an orchestrator as a child and checks the ledger it passes on;
    3. the cache key's host is normalised as the allowlist normalises it (IDNA, lowercase, no
       trailing dot), with the path still as sent;
    4. a version that cannot be recorded removes its artifact;
    5. `ToolScope` refuses a non-canonical `ledger_run_id`.
  - **Declared:** caveat 6.
  - **Per-key serialisation:** the ledger store checks and writes under a lock on (ledger,
    statement key), so concurrent records of one new statement store one claim. M24's
    claim-lock adds singleflight for the work only.
- **DECISION-9945a224** (owner, during M23): **claims cite a fetched URL as well as a version
  uri.**
  - Writing example 20 showed that a live model never sees a version's uri: the fetch result's
    text does not show it, and the provenance manifest is not sent to the provider.
  - So `record_claim` and `supersede_claim` also accept a URL the run fetched. It cites the newest
    tool result in the run's history whose version has that canonical URI, or a final URI that
    canonicalises to it.
  - A URL this run never fetched is refused, even if another run fetched it.
- **Spec and plan re-signing.** The owner permitted the spec and plan to be re-signed as
  `siva-rgb (owner)` twice, each time through the CLI after a `control requeue` of M23. The task
  list is unchanged.

### Requirements (read them verbatim in SPEC.md)

FR-92, FR-93, FR-94, NFR-25, NFR-26, AC-73, and the M23-dated sentences in FR-89, FR-90 and FR-92.

## What changed

**Nothing since M22 is committed.** HEAD is `8ba3b01` (M22). Review with:

```bash
git diff 8ba3b01 -- agentsdk tests scripts README.md SPEC.md
git status --short
```

- **`agentsdk/ledger.py`** (new):
  - `claim_key` (NFC, then casefold, then whitespace collapsed, then SHA-256);
  - `EvidenceClaim` (frozen, validated by field name, `claim_key` must be the key of its
    `statement`);
  - `NewContribution` and `ClaimContribution`;
  - `ClaimView` (sources and labels derived from the claim's contributions);
  - `LedgerStore` and `InMemoryLedgerStore` (one lock for the whole store);
  - `EvidenceLedger`: `active`, `claim` and `history`, oldest first;
  - `RunLedger`, one run's handle: citation checks, provenance, events;
  - `ledger_tools()`, which builds `record_claim` and `supersede_claim`.
- **`agentsdk/migrations/0011_ledger.sql`** (new), adding three tables:
  - `claims`, with `UNIQUE (supersedes)`;
  - `claim_contributions`;
  - `claim_citations`, which references `source_versions`.
- **`agentsdk/postgres.py`**: `PostgresLedgerStore`.
  - `record` and `supersede` each run in one transaction under
    `pg_advisory_xact_lock(hashtext(tenant|project|ledger|key), 3)`.
  - `supersede` takes the old key and the new key in sorted order, and re-checks "already
    superseded" after it holds the lock.
- **`agentsdk/api.py`**:
  - `Runner._ledger_store()` and `Runner.evidence_ledger()`;
  - each run's `ToolScope` gains `agent_id` and a `RunLedger`;
  - `taken_in` is the run's history tool results plus its briefed inputs, read through
    `control.store`.
- **Smaller changes:**
  - `agentsdk/scope.py`: `agent_id`, `ledger`, and caveat 5;
  - `orchestrator.py`: caveat 1;
  - `evidence.py`: caveats 3 and 4;
  - `persistence.py`: `ledger_store()`;
  - `events.py`: `ClaimRecorded` and `ClaimSuperseded`;
  - `__init__.py`: exports.
- **`scripts/20_ledger.py`** (new). **`README.md`**: a feature bullet, example 20's row, and two
  limit rows (caveat 6, and the parallel-batch rule).
- **`SPEC.md`**: FR-89, FR-90, FR-92 and FR-93 edits for the decisions above.
- **`tests/test_ledger.py`** (new, the gate file, 67 tests). `tests/conftest.py` tracks `claims`
  and `claim_contributions`.
- **Edits to approved tests:** `tests/test_distribution.py` lists `20_ledger.py` (FR-56).

### Choices the spec left open, made by the author (judge them)

- **`EvidenceClaim` carries `tenant_id` and `project_id`** besides FR-92's fields.
- **A contribution records `why`**, None except on a supersession's first contribution.
- **`ClaimView.supporting` and `contradicting` hold version ids.** Events and tool results show
  them as `urn:agentsdk:source:` uris.
- **The tool result's content** is JSON: the claim uri, statement, contribution count, sources,
  labels and `new`. Its provenance is raised through `taken_in` by the claim's labels.
- **Superseding a claim with a statement of the same key as the claim itself is allowed.** Only
  *another* active claim with that key refuses.
- **`supersede_claim` takes no `confidence`.** FR-94's signature has none.
- **The in-memory store serialises every write behind one lock,** not one lock per key.
- **`record_claim` is not `concurrency_safe`,** so a response's claim calls run one at a time.

## What the author ran

1. **The gate file**, written before the implementation; 61 passed on the first run.
   - Six tests were added afterwards: three for cite-by-URL, the example's offline run, and two
     strengthenings.
   - The newest-version test first passed for the wrong reason (both fetches got one version).
     It now ticks the clock past freshness on every fetch.
2. **Mutation run: 24 of 24 killed**, each file restored by SHA-256. The mutants covered:
   - each normalisation step of the key;
   - active versus superseded, in memory and in SQL;
   - the already-superseded and matches-another checks, on both stores;
   - labels and sources taken from the last contribution only;
   - taken-in ignored, and cited versions ignored;
   - an unknown citation dropped silently;
   - the oldest version cited by URL;
   - history reversed, and superseded claims shown as active;
   - `new` always true;
   - the Postgres record lock removed;
   - briefed inputs not counted;
   - caveats 1, 3, 4 and 5.

   An equivalent "whitespace" mutant was replaced with one that collapses only spaces.
3. **Gates.** Both green on source hash `fb7c130d`, from one `genesis gate --timeout 590000` run.
   - `unit`: `tests/test_ledger.py`, exit 0, 67 passed in 5.95s.
   - `regression`: the full suite, exit 0, 1864 passed in 346.55s (19:06:02 to 19:11:49 UTC). The
     baseline before M23 was 1797.
4. **Example 20.**
   - Offline: 7 of 7.
   - Live, against example.com, iana.org, the gateway and Postgres: 6 of 6 on the first attempt.
   - The live run's rows were removed by run id. The store is at baseline: example-tenant has 19
     runs, there are 6 artifacts, 0 claims, contributions, citations and source versions, and
     nothing is running.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m23-r1-probes` in your own session's scratchpad, with tenant ids
starting `SYN-m23r1`; delete only rows your probes wrote, by the run ids they recorded.

1. **Dedup.**
   - Does every pair AC-73 names (case, spacing, NFC/NFD, "ß"/"ss") give one key?
   - Is there an input where `claim_key` differs between the two stores or two processes?
   - What do zero-width characters, non-breaking spaces and full-width forms do, and does that
     matter?
2. **Concurrency.** Concurrent records of one statement, concurrent supersessions of one claim,
   and a record racing a supersession of the same statement's claim, on both stores at
   1 microsecond.
   - Is there one claim, a chain that never forks, and no contribution left on a claim that was
     already superseded when it was written?
   - Can the sorted two-key lock in `supersede` deadlock against `record`?
   - Two processes on one database: `hashtext` can collide; does that cost anything but waiting?
3. **Taint (P3-D11, ADR-26).**
   - Can any contribution or claim end up less tainted than a version it cites, or than what its
     run had taken in?
   - Does a compaction summary, a briefed input, or a child's answer reach `taken_in` as FR-93
     requires?
   - Is the tool result's provenance raised as stated?
4. **Tenancy (NFR-25).**
   - Can a claim cite another tenant's or project's version, by uri or by URL?
   - Can a ledger be read, or a claim superseded, from another tenant, project or ledger?
   - Read every SQL statement in `PostgresLedgerStore` for its filters.
5. **Cite-by-URL.**
   - Is "the newest version this run was given" right after a redirect, a cache hit, a copy, and
     a refetch?
   - Can a URL resolve to a version the run never took in?
   - Is a URL that only another run fetched refused?
6. **Append-only (NFR-26).** Is there any path that updates or deletes a claim, contribution or
   citation? Are the store-side checks and constraints sufficient if a caller bypasses
   `RunLedger`?
7. **The M22 caveats.**
   - Are 1 to 5 sound and tested?
   - Is 6 declared faithfully?
   - Did the caveat 3 change alter any M22 behaviour beyond cache keys (for example
     `final_uri`, or `schema_hash`)?
8. **Governance and regressions.**
   - The two re-signings, `git diff 8ba3b01 -- .genesis/project.json`: no task changed?
   - The approved-test edit.
   - Example 20 offline.
   - The full suite.

## Declared limitations: known, recorded, NOT findings

- **Results of other calls in the same parallel batch** are not yet in the run's history and do
  not count toward a claim's labels (FR-93, README).
- **Caveat 6:** a fetch tool widened to a broader scope keeps serving the run's own narrower
  version until it expires (README).
- **`http://h` and `http://h/` are different cache keys:** the path is kept as sent (FR-89,
  DECISION-9de80b77).
- **M24 adds singleflight for the work.** Until then two agents may each fetch, but their claims
  merge.
- Everything declared for M20 to M22 still stands (KNOWLEDGE-cc4d686c, KNOWLEDGE-afac039f,
  KNOWLEDGE-8e29cfe3, KNOWLEDGE-bbd190e1, KNOWLEDGE-2763f57b), except the caveats this settles.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. Latent, out-of-scope or
  cosmetic findings are caveats in your reason, not blockers.
- **Restore every file you mutate** and verify SHA-256, restoring in a `finally`.
- **Clean up** every run, row, temp folder and process you create, and say what you removed.
  Ledger rows go first: citations, then contributions, then claims (newest first), then versions
  and runs.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M23-evidence-ledger --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M23-evidence-ledger --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the eight attack items, and
your verdict.
