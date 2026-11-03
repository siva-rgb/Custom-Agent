You are the independent L4 reviewer for a bounded change in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is M45 round 4.** Use a fresh session: **Fable 5.1**, falling back to Opus 5 only if Fable 5.1
fails to start. Not Sonnet 5.5 (round 3), not Opus 5.5 (the author). Bring no memory of an earlier
review: if your session holds any, say so and ask for a fresh one.

Rounds 1, 2 and 3 all **rejected** M45 on NFR-39, through three different edges (below). Each time,
the MCP engine reached the Runner through the Runner's `WorkspaceManager`. The engine is held by a
module-global `weakref.finalize` registry, and the manager holds run state by design. Each earlier
repair cleared one route at one moment. **Round 4 reviews a change of mechanism, chosen by the owner
(DECISION-8e21d1ff):** the engine holds no strong reference to the Runner's manager, and the two
things it shares with that manager, the slots and the provider, hold no run strongly. Your job is to
find any strong route from the engine to run state that remains, in any shape: ended, abandoned,
mid-command, on either provider. Then review M45 as a whole.

Round 1's brief, `.genesis/reviews/M45-review-prompt.md`, holds every requirement quoted verbatim from
SPEC.md, plus the records and the Repository, Running tests and Rules sections. They all apply here
unchanged, with one correction: **the cleanup order gains `mcp_tool_acceptances` and
`mcp_tool_versions`** (by your own tenancy).

The rules:
- Use tenant ids starting `SYN-m45r4`.
- Run one test session at a time.
- Never pull, build, prune or stop Docker objects.
- Never print credentials.
- Never pass an unrecognised flag to genesis.
- Restore any mutant byte for byte, checked by SHA-256, after asserting it applied.
- The owner's stopping rule (DECISION-c697f36c): a round that approves with nothing blocking is the
  last. So report everything, and mark what you would block on.

`.genesis/KICKOFF.md` reads `independent-review:pending` after a reject, so the gates line alone
does not show that a review ran: read the controls for rounds 1 to 3.

## Every finding so far, and its answer

| Round | Finding | Repair |
|---|---|---|
| Pre-review | F1 to F7: the entrypoint kept; the start and close bounds; untested slot, egress, trigger and closed manager; example 39's folder; file/folder and case clashes; a timed-out record write; a dead server's workspace | DECISION-5470ab57, as round 1's brief records. |
| 1 | **B1 (blocking):** the manager, handed to the engine, held the Runner's bound `_workspace_store`. | DECISION-7a3e4526: a store getter that holds no Runner (kept). |
| 1 | N1 late record row; N2 example's neighbour; N3 the warning filter's breadth; N5 `run_id` types | Repaired (DECISION-7a3e4526). N4 (0026's trigger on INSERT only) declared in the README. |
| 2 | **B1 residual (blocking):** a *kept* workspace's command binding reached a dropped Runner. | DECISION-3756a232: `settled()` released a run's binding at its end. **Withdrawn in round 3**: with no engine edge it guards nothing. |
| 2 | NB1 late record follow-up; NB2 example's neighbour; NB3 and NB4 declared | Repaired or declared as round 3's brief records (kept). |
| 3 | **B1 (blocking):** a Runner dropped mid-run never reached `settled()`; its binding still reached it. A plan's top-level binding the same, and its release was an unguarded mutant. | **DECISION-8e21d1ff:** `use_workspaces` gives the engine `manager.for_servers()`, a manager of its own that shares the provider, store getter, owner, Runner label and slots, holds no run, and reaches the Runner's manager only through a weak reference (for its closed check). The slots hold each waiter weakly. A provider's egress watcher holds its manager and workspace weakly. `FakeProvider` keeps its running commands' tasks and kill events in weak-value maps; they stay pending after a run's loop is closed and led back to the run. The `WorkspaceProvider` docstring states the rule for any provider (DECISION-acc02de9). |
| 3 pre-review | **B1:** the fake egress `Proxy.handlers` set held the command's task (on the fake, a handler *is* that task), so a run abandoned mid-connection pinned its Runner. **B2:** Docker's `running` map reached the run through each command's kill event once its container ran. The author had left that map strong because a mutant survived, but the test was vacuous: its readiness check fired before the container ran. | DECISION-26bac515: `Proxy.handlers` is a `WeakSet`; Docker's `running` is a weak-value map; the Docker test waits until the kill is awaited; a mid-connection shape was added. A sweep of every provider-held asyncio object found nothing else. |
| 3 pre-review | S1 a failing walk's paths lost; S2 noted reports never checked; S3 the closed check after a server's slot is taken untested; a slot waiter whose loop was closed granted (the give then raised); a server's late count never waited for | Each repaired with a test (DECISION-26bac515). |
| 3 | NB-C: the walker missed keyword defaults, a function's attributes and class attributes | Followed now, and a test shows each kind is found and a weak reference is not. |
| 3 | NB-D: `_close_all` awaited `trailed()` unguarded | Guarded, with a test that a failing wait still ends the loop's tasks. |

## How the author checked it

- **One test per shape and mode:**
  `test_whatever_its_run_did_the_engine_reaches_no_run_state_and_its_runner_is_collected`.
  - Shapes, run to their end: plain, kept, kept with egress, plan, plan with egress, egress, failed,
    cancelled, waiting for a slot.
  - The same shapes abandoned (loop closed with the run under way, the Runner dropped, never
    closed), plus mid-command, mid-command with egress, and mid-connection (a CONNECT head never
    finished, so the fake proxy's handler is pending).
  - The run's loop is closed first, which is NFR-39's condition. A live loop is a root of its own
    and reaches everything it schedules.
  - Then, with the Runner still held, the walk from the engine must reach none of these: a Runner,
    a run control or handle, a sink, a `RunWorkspace`, a slot waiter, or a manager that is not a
    server side.
  - The Runner is then dropped, must be collected, and its server's workspace must be removed.
  - A Docker-marked test runs the same check in a process of its own, for a run abandoned
    mid-command (its container running, its kill awaited), with and without egress. The parent
    then reaps what that process left.
- **No warning filters.** While the dropped Runner is collected, and while its workspaces are
  removed afterwards, `_reports_as_text` keeps unraisable reports and warnings as text only.
  - Why: pytest keeps each report's object (an unraisable's object, a warning's source), and that
    brought the Runner back during the very collection under test.
  - On Docker this happened from threads and timers outside any window the test could guard,
    which is why that check runs in its own process.
  - Each noted report must be one of the expected closed-loop kinds (`EXPECTED_REPORTS`);
    anything else fails the test. Check that the allowed set is not too broad.
- **Mutation:** 72 mutants, all killed. Round 2's two release mutants were retired with the
  release. Round 3's mutants:
  - the engine given the Runner's own manager;
  - the server side holding the manager strongly, keeping its own slots, or using another label;
  - a slot waiter held strongly, or a gone waiter granted the slot;
  - a watcher holding its manager or its workspace;
  - the fake's running or kill map, the proxy's handlers, or Docker's running map made strong;
  - the closed check after the slot removed; a closed-loop waiter granted; the late count not
    waited for; the report check removed;
  - the trailing wait unguarded;
  - each walker gap.
- **Gates** on source hash `434f60500e228147b528dca271148a7ba2e35baf60e55aeb7d85cd0bf6dbc86e`: unit 80 passed (13 Docker cases deselected), regression 3223 passed, docker 128 passed, each exit 0. A first gate run failed regression on five older
  tests:
  - `tests/test_workspace_tools.py`'s `_running` helper put a Docker command into the now weak
    `running` map with nothing else holding it. The helper now holds it, as a command's task does.
  - The MCP bounds test (`tests/test_mcp_client.py`) wanted `_close_all`'s new `suppress` and
    `use_workspaces`'s `for_servers` call listed.

  Check that neither repair weakens what those tests guard.

## Attack

1. **The mechanism.** Is there any strong route left from the engine to run state? Places to look:
   - something else a provider or the egress proxy keeps per workspace, command or connection
     (the author's sweep says the Docker reader and proxy process are awaited only after removal
     has taken the space out of the provider's map; check that);
   - the store getter on PostgreSQL;
   - the catalog;
   - the MCP loop's own tasks;
   - a third-party provider. The README declares what a provider must not hold. Is that enough?

   Break each weak edge at runtime and confirm a test fails.
2. **The withdrawal of the release.** Rows written after a run ends, for a kept or plan workspace,
   now go through that run's store call again, as M44 approved them. Check M44's tests and example
   38 still hold, and that no event follows a terminal event.
3. **The server side.** Check whether it sweeps, reaps or closes anything the Runner's own manager
   should. Check the slot accounting shared across the two. Check what happens when the Runner's
   manager is collected first.
4. **Everything round 1 attacked**, wherever these repairs touched it.

## Recording your decision

Single quotes around `--reason`; no apostrophes, backticks or `$` in it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M45-sandboxed-mcp --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M45-sandboxed-mcp --human '<your name>' --reason '<the defect>'
```

Do not record anything while a gate runs. Then report:
- what you checked and what you ran;
- every finding, marking which you would block on;
- declared gaps;
- what you cleaned up, by table, the MCP catalog tables included;
- your verdict.

Do not commit or publish anything.
