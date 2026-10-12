You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Opus 5, falling back to Sonnet 5** if Opus 5 fails to
start. M26a was implemented by Opus 5.5. Earlier reviews: Fable 5.1 approved M26 and raised the
caveats this settles, Sonnet 5 approved M25a, and Opus 5 approved M25. Bring no memory of an
earlier review into this one: if your session holds any, say so and ask for a fresh one.
Re-derive the requirements from SPEC.md ("#### M26a": FR-110, and AC-83), and from the parts of
FR-102, FR-103 and AC-78 it amends, rather than inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.** Pass every DSN
  through `agentsdk.config.normalise_database_url`. A raw `postgresql+psycopg://` DSN makes
  psycopg echo it in its error.
- **Host:** Windows Developer Mode must be on (D7). Python 3.11.

**Running tests**
- **Only one test session at a time, and nothing else in parallel with one.** Every pytest session
  compares the store's ids at its start and end (AC-44), and the development database is shared.
- Check that no other python or node process is running first.
- Run your own probes with `-p no:cacheprovider`: Genesis hashes .pytest_cache.
- **The full suite now takes about 8 minutes**, against the gate's 9m50s limit. Keep the machine
  idle while a gate runs, and split your own full-suite runs into halves under 590 s each.

**Gates:** `genesis gate` kills a gate after 120 s unless you pass `--timeout 590000`, and on this
host that kills only the venv launcher (KNOWLEDGE-64fa6e18). Run
`genesis gate . M26a-gate-caveats --timeout 590000`, then check for a stray python process.

**Mutations:** restore a mutated file byte for byte and verify its SHA-256 in a `finally`. Beware
`git stash`: on this host it rewrites line endings (KNOWLEDGE-ae83f412). **Do not mutate an
applied migration file.** Its checksum is recorded in the development database, so every test
then fails for the wrong reason. Mutate a scratch schema instead.

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown.

## Task under review

**M26a-gate-caveats**, **round 1**, risk medium. M26 was approved at `eb69b1d` with four caveats
(KNOWLEDGE-306e8bfc). The owner had them settled before M27 (DECISION-512ecd10).
- The spec and plan were signed as `siva-rgb (owner)` with permission.
- M27 and M28 were paused through the CLI, as for M25a, and will be resumed and requeued after
  this.
- No state was edited by hand.

### What it settles

1. **FR-103 stated as it holds.**
   - No model can record an outcome: `record_validation` is in no tool registry, and no shipped
     tool calls it.
   - A custom tool reaches the Runner through `_runner`. It is first-party code in the process, so
     it is declared outside the threat model (DECISION-2bad84bb) in the README.
   - The test now walks the *values* of a tool scope's fields. It fails if any field but `_runner`
     holds the Runner or a writer of verifications, and is shown to fail when one is planted.
   - It runs every shipped tool with `record_validation` watched: fetch, record, supersede, and
     `run_plan` through a gated plan.
   - A static check finds `record_validation` in no module but `api.py`.
   - M26's name-only test, `test_record_validation_is_reachable_from_no_tool`, is left as it was
     approved. Judge whether its name now misleads.
2. **Migration `0013`.** It makes `0012`'s comment true for claim subjects:
   - `claims` gains UNIQUE `claims_ledger_identity` (claim_id, tenant_id, project_id,
     ledger_run_id);
   - `verifications` gains a composite foreign key `verifications_claim_in_ledger` to it, MATCH
     SIMPLE, so node subjects are unaffected.
   - It is idempotent through `pg_constraint` checks in a DO block. `0012` is not edited.
3. **README rows** for the criteria order and for the custom-tool limit.
4. **`low_confidence` on a claim only a critic vouched for.** It now requires any contribution at
   all, rather than any non-critic one, so a claim only a critic vouched for fires, and a claim
   with no contributions still does not.

## What changed

**Nothing since M26 is committed.** HEAD is `eb69b1d`. Review with:

```bash
git diff eb69b1d -- agentsdk tests README.md SPEC.md
git status --short
```

- `tests/test_gate_caveats.py` (new, the gate file, 8 tests). **Edits to approved tests:** none.

## What the author ran

1. **The new tests against M26's code:** 4 of 8 fail, the four repairs.
   - The bypass test against M26's schema failed with "DID NOT RAISE ForeignKeyViolation": the
     cross-ledger, cross-tenant and cross-project rows landed.
   - The other four pin FR-103 as it already holds, by value.
2. **Mutation run: 4 of 4 killed.** The mutants:
   - both edges of the new rule;
   - the Runner planted in `_control`;
   - a shipped tool (the gate) calling `record_validation`. Only the static check caught this
     one, because `-x` stopped there.

   `0013` was not mutated in place, for the reason above. Its teeth are the failing bypass test
   on M26's schema.
3. **Gates.** Both green on source hash `22f32a7f`, from one `genesis gate --timeout 590000` run.
   - `unit`: 8 passed in 2.64s.
   - `regression`: 1989 passed in 486.48s (09:42:13 to 09:50:22 UTC). The baseline before M26a
     was 1981.
4. **Store:** at baseline. 0 advisory locks.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m26a-r1-probes` in your own session's scratchpad, with tenant
ids starting `SYN-m26ar1`; delete only rows your probes wrote, by the run ids they recorded.

1. **FR-103 / AC-83.**
   - Does the walk catch the cases that matter, and is one level deep the right depth?
     `ledger` is a `RunLedger` whose `_store` is a bound method of the Runner, the same declared
     reach.
   - Can a model, through any shipped tool, record an outcome?
   - Is the README's declaration accurate?
2. **`0013`.**
   - With the store's check bypassed, can any verification name a claim outside its tenant,
     project or ledger?
   - Is it idempotent, and do all checksums match where it is applied?
   - Does it break a node subject, or a claim row written before it?
3. **The signal rule.** Check it against FR-102 as amended: the critic exclusion, a claim with no
   contributions, and the M26 oracle.
4. **Regressions:** the gate files of M26 and M22 to M25a, and the full suite, in halves.

## Declared limitations: known, recorded, NOT findings

- **The critic is not built** (M27).
- **Everything declared for M20 to M26 still stands:** W1, C6, B2, a waited fetch that raises
  recording no wait, `KeyFailed`'s context and pickling, M25a's untested M4 branch, the claim set
  and `gate_all_nodes` rows.
- **A custom tool can call `record_validation`** (declared by this task).

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. Latent, out-of-scope or
  cosmetic findings are caveats in your reason, not blockers.
- **Restore every file you mutate** and verify SHA-256, restoring in a `finally`.
- **Clean up** every run, row, temp folder, process and advisory lock you create, and say what you
  removed.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M26a-gate-caveats --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M26a-gate-caveats --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the four attack items, and
your verdict.
