You are the independent L4 reviewer for a bounded change in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is M45 round 3.** Use a fresh session: **Sonnet 5.5, falling back to Fable 5.1** if Sonnet 5.5
fails to start. Not Opus 5 (round 2), not Opus 5.5 (the author). Bring no memory of an earlier review:
if your session holds any, say so and ask for a fresh one.

Rounds 1 and 2 both **rejected** M45 on the same requirement through two different edges (below).
**Round 3's focus is that requirement as a property, not as an edge**: NFR-39 holds only if nothing the
MCP engine can reach -- it is held by a module-global `weakref.finalize` registry -- leads back to a
Runner. Try to find any route the author's property test does not walk. Then review M45 as a whole.

Round 1's brief, `.genesis/reviews/M45-review-prompt.md`, holds every requirement quoted verbatim from
SPEC.md, the records, and the Repository, Running tests and Rules sections, which apply here
unchanged, with one correction: **the cleanup order gains `mcp_tool_acceptances` and
`mcp_tool_versions`** (by your own tenancy), which round 1's omitted -- round 2's reviewer left 38
`mcp_tool_versions` rows the orchestrating session then removed. Use tenant ids starting `SYN-m45r3`.
One test session at a time; never pull, build, prune or stop Docker things; never print credentials;
never pass an unrecognised flag to genesis; restore any mutant byte for byte, by SHA-256, after
asserting it applied. The owner's stopping rule (DECISION-c697f36c): a round that approves with
nothing blocking is the last -- so report everything, and mark what you would block on.

## Every finding so far, and its answer

| Round | Finding | Repair |
|---|---|---|
| Pre-review | F1 to F7: the entrypoint kept; the start and close bounds; untested slot, egress, trigger, closed manager; example 39's folder; file/folder and case clashes; a timed-out record write; a dead server's workspace | DECISION-5470ab57, as round 1's brief records. |
| 1 | **B1 (blocking):** a Runner with a sandboxed server was never collected: the manager, handed to the MCP engine, held the Runner's bound `_workspace_store`. | DECISION-7a3e4526: a store getter holding no Runner. |
| 1 | N1 late record row; N2 example's neighbour; N3 the warning filter's breadth; N5 `run_id` types | Repaired (DECISION-7a3e4526). N4 (0026's trigger on INSERT only) declared in the README. |
| 1 | A gate flake outside M45 | M35's `Served` helper retries its port file's read, and kills its server if its start fails (it had leaked one for a day). |
| 2 | **B1 residual (blocking):** a *kept* workspace still held a dropped Runner: engine -> manager -> kept space -> command binding -> StoredEmit -> PublishingSink -> RunHandle -> Runner. | **DECISION-3756a232.** Why it recurred: round 1 was repaired as an edge, not the property, and its test covered one run shape. Now `settled()` releases a run's binding -- its event writer and its run's store call, replaced by no event and a direct store write -- in a `finally`, once its gate is shut, so an ended run leaves nothing in the manager that reaches it. |
| 2 | NB1: the late record's follow-up kept but untested, and never waited for at close | The follow-up is tracked from the moment it is scheduled (`_removed_after`), and the engine's close waits for it within `_REMOVE_SECONDS` before ending its loop's tasks (`WorkspaceManager.trailed`). |
| 2 | NB2: example 39's neighbour made before its `try` | Made inside it, in both modes; `neighbour_of` removes what it made if it then fails. |
| 2 | NB3: pytest's session-end unraisables bypass a per-test filter | Declared: inherent to a per-test mark; nothing M45 changed reaches it. |
| 2 | NB4: a double removal record possible | Declared harmless: both stores' `record_removal` returns False once `removed_at` is set. |

**The requirements trace** the author then ran found three gaps, each now tested: the *stored* manifest
records a sandboxed server's spec and file names (FR-164); a sandboxed server's results keep FR-125's
untrusted MCP labels (FR-166); a plain stdio server beside a provider makes no workspace, row or
folder (NFR-49).

## How the author checked it

- **The property test** (`test_whatever_its_runs_did_the_mcp_engine_reaches_no_run_state`): for eight
  run shapes -- plain, kept, kept with egress, plan, egress, failed, cancelled, waiting for a slot -- it
  walks everything the engine reaches (objects, closures and bound methods; not function globals) with
  the Runner still held, and asserts it reaches no `Runner`, `RunControl`, `RunHandle`,
  `PublishingSink` or `InMemoryEventSink`. A collection test runs round 2's kept shape end to end. A
  probe before the repair found the kept route; after it, all seven shapes it ran were collected.
- **Mutation:** 51 mutants, all killed. Round 2's: the release removed, or only its
  event writer released (both killed by the property test); the close not waiting for a late record;
  the late record not kept; every server sandboxed (killed by the NFR-49 test).
- **Gates** on source hash `1bc606d3`: unit 61 passed, regression 3,204 passed (30:36), docker 126 passed (46:17), all exit 0 with no warnings, kept awake.

## Attack

1. **The property.** Is there any shape the eight miss -- a run still under way when its loop is closed,
   a plan whose top-level run never ended (`_plans` and `_Space.owner`), a command still running, a
   server dead or restarting, an egress recorder's late pass, `release_workspace`, two Runners sharing a
   provider, PostgreSQL persistence, Docker? Does the walk itself skip a kind of reference it should
   follow? Break the release at runtime and confirm the test fails.
2. **The release** must not break M44: a kept workspace's later connection rows under its ended run,
   still written; no event after a terminal event; the egress recorder's booking order.
3. **NB1:** can `trailed()` wait on a loop that is closing, or miss a follow-up scheduled after it began?
4. **Everything round 1 attacked**, for anything these repairs touched.

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

Do not record anything while a gate runs. Then report what you checked, what you ran, every finding
(marking which you would block on), declared gaps, what you cleaned up -- by table, the MCP catalog
tables included -- and your verdict. Do not commit or publish anything.
