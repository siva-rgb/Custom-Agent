You are the independent L4 reviewer for one small change in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is M44 round 7, a short review of the change since round 6.** Use a fresh session: Opus 5,
falling back to Sonnet 5.5 if Opus 5 fails to start. Not Fable 5.1 (round 6), not Haiku 5.5, not
Opus 5.5 (the author). Bring no memory of an earlier review: if your session holds any, say so.

Round 6 (Fable 5.1) **approved** M44 at source hash `f1ef275c` with six non-blocking
observations. The owner's standing rule is that nothing carries to the next milestone, so all six
were closed in M44 (DECISION-5445c8d0). Only one changed code; the source hash moved, so the
approval needs this review. The full round 6 brief, with every requirement quoted verbatim and the
whole history, is `.genesis/reviews/M44-review-prompt.md`: read its Repository, Running tests and
Rules sections, which apply here unchanged (one test session at a time; gates in the background,
kept awake; never pull, build, prune or stop Docker things; touch only your own `SYN-m44r7` tenancy;
never print credentials; never pass an unrecognised flag to genesis).

## The change

1. **`agentsdk/handle.py`, `through()`** (observation 1): a cancel held while the work ran was lost
   when the work then raised an exception, because only `CancelledError` was caught around
   `asyncio.shield(work)`. Now the work's own exception is caught too (the work is then done), and a
   held cancel always wins: `CancelledError` is raised chained from the work's exception, so neither
   is lost. With no held cancel the work's result or exception is returned or raised as before.
   `through()` guards `WorkspaceManager.settled()` and `Runner._end_workspace()`, and M45 to M47
   inherit it.
2. **`tests/test_egress.py`**: `test_r6_through_keeps_a_held_cancel_when_its_work_then_fails`.
3. **README.md**, wording only: observation 2 (a run's end is bounded unless the store hangs
   because both shipped stores serialise a run's writes; a custom store that does not is named),
   observation 4 (the stop flag also ends a recorder's last counting pass past `aclose()`'s bound),
   observation 5 (the proxy's own container name resolves through 127.0.0.11, declared), and
   observation 6 (a ClientHello spanning more than one TLS record is refused `no_sni`, fails closed,
   declared).
4. **DECISION-5445c8d0** records all six, correcting the wording of DECISION-5c68ddfb (observations
   2 and 3) and DECISION-7a3a2ae2 (observation 4). No SPEC change: FR-162's wording already matched.

## How the author checked it

- The new test passes, and two new mutants on it are killed: dropping the new `except Exception`
  (the held cancel lost) and dropping the chaining. The three round 5 `through()` mutants are
  still killed. 125 mutants in all.
- Gates on source hash `413ffdd1`: unit 164 passed, regression 3,135 passed (30:47), docker 115 passed (43:22), all exit 0, kept awake.

## Attack

1. **`through()`**: can it now lose a cancel, raise two, swallow a `BaseException` it should not
   (`KeyboardInterrupt`, `SystemExit`), leave the work running unowned, or change what its two
   callers did before? Break the change at runtime and confirm the new test fails.
2. **The README rows and DECISION-5445c8d0**: does each say what the code does? Check observation 2's
   claim against `agentsdk/postgres.py`'s advisory lock and the in-memory sink.
3. **Anything else the change touched.** Run the egress tests and confirm the gate evidence is fresh
   at the current hash.

## Recording your decision

Single quotes around `--reason`; no apostrophes, backticks or `$` in it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M44-egress --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M44-egress --human '<your name>' --reason '<the defect>'
```

Do not record anything while a gate runs. Then report what you checked, what you ran, any finding
(marking which you would block on), and your verdict. Do not commit or publish anything.
