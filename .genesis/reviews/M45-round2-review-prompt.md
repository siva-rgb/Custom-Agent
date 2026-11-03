You are the independent L4 reviewer for a bounded change in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is M45 round 2.** Use a fresh session: **Opus 5, falling back to Sonnet 5.5** if Opus 5 fails
to start. Not Fable 5.1 (round 1), not Opus 5.5 (the author). Bring no memory of an earlier review:
if your session holds any, say so and ask for a fresh one.

Round 1 (Fable 5.1) **rejected** M45 on one blocking defect and five others; the owner chose the
repairs (DECISION-7a3e4526), below. **Review those repairs first, then M45 as a whole**: round 1's
brief, `.genesis/reviews/M45-review-prompt.md`, holds every requirement quoted verbatim from SPEC.md,
the records, the Repository, Running tests and Rules sections -- all of which apply here unchanged
(tenant ids `SYN-m45r2`; one test session at a time; never pull, build, prune or stop Docker
things; never print credentials; never pass an unrecognised flag to genesis; restore any mutant byte
for byte, by SHA-256, after asserting it applied). The owner's stopping rule (DECISION-c697f36c): a
round that approves with nothing blocking is the last; so report everything, and mark what you would
block on.

## Round 1's findings, answered (DECISION-7a3e4526)

| Finding | Repair |
|---|---|
| **B1 (blocking):** a Runner with a sandboxed server was never garbage-collected: the WorkspaceManager, handed to the MCP engine, held the Runner's bound method `_workspace_store`, so the engine's `weakref.finalize` registry kept the Runner alive and NFR-39's finalizer never ran. | The manager gets a store getter holding no Runner -- the persistence's own `workspace_store`, or a closure over the in-memory store -- as the catalog's already was (`agentsdk/api.py`). A test runs a turn, lets its loop close, drops the Runner without closing it, collects it, and sees its server process gone, its folder removed and its row removed `aclose`. Every other object the engine holds (servers, registered names, the catalog getter, the provider) was checked for a reference back to the Runner. |
| **N1:** a record write past its bound that then landed left an unremoved row for a removed workspace. | `open_server` keeps the write's future; if it is still under way when the workspace is removed, its removal record follows it when it lands (`_trail`). Tested with a write that lands after its bound. |
| **N2:** example 39's neighbour workspace had no owner label and was removed outside a `finally`. | It carries `agentsdk.owner`, and is removed in a `finally` in both modes. |
| **N3:** the `test_compute` warning filter silenced every unraisable warning. | Narrowed to `Exception ignored in.*Transport.__del__`. |
| **N4:** migration 0026's connections trigger fires on INSERT only. | Declared (owner): no SDK path updates a row's run or scope; README row. |
| **N5:** `WorkspaceRecord.run_id` and `ConnectionRecord.run_id` typed `str`. | Typed `str | None`. |
| **N6, N7** (notes): `pytest.raises(match=...)`; the proxy token on the `docker run -i` line. | Unchanged, as round 1 left them to the author: FR-164 requires the message to name `WorkspaceSpec.env`, and the token is as M44's (DECISION-88dea779). |

## An edit to an approved test helper since round 1

`tests/test_mcp_http.py` (M35), `Served.__init__`: the first round-2 regression gate failed one test of
3,191, outside M45 -- `test_mcp_input.py::test_a_form_elicitation_pauses_the_call_and_an_accepted_answer_continues_it[modern-postgres]`
-- with `PermissionError` reading its fixture server's port file, which that server writes to a
temporary file and renames into place; Windows briefly refuses an open as a file is replaced or
scanned. The helper now retries the read on `PermissionError` (and on `FileNotFoundError`, as its
wait for the file did) within its existing 60 s start deadline (the owner's choice). And the failure
had leaked its fixture server: `__init__` raising left the process it started with no teardown, and
it was found still holding its port the next day. Any failure in `__init__` now kills that process
before raising. Nothing it asserts was loosened. The only other wait-then-read on a file a fixture process writes was swept
for: there is none.

## How the author checked it

- The B1 probe, before and after: before, a sandboxed Runner `collected: False`, its engine alive;
  after, `collected: True`, no engine left, as a plain one.
- **Mutation:** 46 mutants, all killed, two new: the store getter bound to the Runner
  again (killed by the collection test) and the late record left unfollowed.
- **Gates** on source hash `40793b22`: unit 48 passed, regression 3,191 passed (29:32), docker 126 passed (43:55), all exit 0 with no warnings, kept awake.

## Attack

1. **B1:** can a sandboxed Runner still be kept alive -- by the egress recorder, a late pass, `_trail`,
   the provider, a running server, the Runner's own event loop still open, or PostgreSQL persistence?
   Does collection still end the server and remove its workspace on Docker?
2. **N1:** can `_trail`'s task be lost, run on a closed loop, or record a removal for a workspace that
   was never removed?
3. **Everything round 1 attacked**, for anything these repairs touched: lifetime, two loops, records,
   trust, bounds, the example and the tests.

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
(marking which you would block on), declared gaps, what you cleaned up, and your verdict. Do not commit
or publish anything.
