You are the independent L4 reviewer for one small change in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is M44 round 8, a short review of the change since round 7.** Use a fresh session: Sonnet
5.5, falling back to Fable 5.1 if Sonnet 5.5 fails to start. Not Opus 5 (round 7), not Haiku 5.5,
not Opus 5.5 (the author). Bring no memory of an earlier review: if your session holds any, say so.

Round 7 (Opus 5) **approved** M44 at source hash `413ffdd1` with three non-blocking findings. The
owner's standing rule is that nothing carries to the next milestone, so all three were closed in
M44 (DECISION-ed9b1bba); the source hash moved, so the approval needs this review. The full brief,
with every requirement quoted verbatim and the whole history, is `.genesis/reviews/M44-review-prompt.md`;
round 7's is `.genesis/reviews/M44-round7-review-prompt.md`. Their Repository, Running tests and
Rules sections apply here unchanged: one test session at a time; gates in the background, kept
awake; never pull, build, prune or stop Docker things; touch only your own `SYN-m44r8` tenancy;
never print credentials; never pass an unrecognised flag to genesis.

**A lesson from round 7, for your mutants:** `docker.py` and `manager.py` have LF line endings in
the working copy, other files CRLF. A mutant whose edit silently never applies looks exactly like
a surviving one, so assert that the file actually changed before running its test, and restore it
byte for byte (SHA-256).

## The change

1. **F-R7-1:** `agentsdk/workspace/docker.py`'s `_through` had the shape `agentsdk.handle.through`
   had before round 6's repair: a cancel held while the work ran was lost when the work then raised
   (reproduced at the cleanup in `_contained`'s `finally`, where a bounded wait on a `docker start -a`
   client that outlived its kill timed out and the call site's `suppress(Exception)` ate it). The
   author found a third copy with the identical body, `agentsdk/workspace/manager.py`'s `_completed`
   (nine call sites; docker's had seven). Both predate M44. **Both are deleted; every call site now calls
   `handle.through`**, imported in each module.
2. **Tests** (`tests/test_egress.py`): `test_r7_the_workspace_modules_hold_a_cancel_through_the_one_helper`
   (both modules hold `handle.through`, neither its copy) and
   `test_r7_a_command_cancelled_while_its_docker_client_outlives_its_kill_ends_cancelled`, which
   drives `_contained` with a stand-in `docker start -a` client that never exits and a lowered
   `_CLI_SECONDS`, cancelling during the cleanup's bounded wait: the task must end cancelled.
3. **README.md**, wording only: F-R7-2 (a recorder's last counting pass is never cancelled; once
   `aclose()` sets the stop flag, which it does only when a late count is still running past its
   bound, it stops after its current write) and F-R7-3 (Docker's resolver answers the names of every
   container on the workspace's own network, not only the proxy's; no new reach).
4. **DECISION-ed9b1bba** records all three, correcting DECISION-5445c8d0's wording.

## How the author checked it

- Two new mutants, each restoring the pre-repair copy under the module's name: in `docker.py`
  killed by the `_contained` test alone; in `manager.py` killed by the routing test (no manager call
  site was driven to a work failure after a held cancel; the behaviour itself is round 6's
  `through()` test). 127 mutants in all.
- Gates on source hash `1d1809cb`: unit 166 passed, regression 3,137 passed (32:12), docker 115 passed (45:45), all exit 0, kept awake. **The docker gate reported 2 warnings**, which rounds 5 to 7's gates did not: an unclosed `docker` client transport (`PytestUnraisableExceptionWarning`, `ValueError: I/O operation on closed pipe` in its `__repr__`) from `tests/test_compute.py::test_on_docker_the_cancel_check_without_a_layer_and_its_cover_fails[provider kill]`. The author ran that test alone 4 times: 2 on this code and 2 with both pre-repair copies restored. Every run passed with 1 warning, so the change did not cause it. The test removes both the provider-kill layer and its cover on purpose, so the client it starts is never killed; earlier gates missed the warning only by garbage-collection timing. The owner chose to leave the test as it is and have you judge the warning: say whether it matters, and whether it hides anything else.

## Attack

1. **The routing:** does every former call site behave as before when nothing fails, including
   ones that passed a task rather than a coroutine (`through` calls `ensure_future`, which returns a
   task unchanged)? Can any now raise where it did not, or end early? Break the change at runtime and
   confirm the tests fail.
2. **Any other copy:** search `agentsdk/` for any other hold-a-cancel loop around `asyncio.shield`
   with the same flaw. The author found `claimlock._completed` keeps what was delivered and
   `postgres.py`'s artifact loop lets the cancel win; check both.
3. **The README rows and DECISION-ed9b1bba:** does each say what the code does?
4. **Gate evidence:** fresh at the current hash.

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
