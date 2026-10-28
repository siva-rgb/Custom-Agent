# Custom Agent SDK

A Python SDK for building tool-using AI agents on your own terms: no vendor agent
framework underneath, every tool call validated and permission-checked before it
runs, every result tagged with where it came from, and every run optionally
persisted so you can reconstruct exactly what happened afterwards.

> **Status: `0.1.0.dev0`, pre-release.** Phase 0 (the single-agent foundation) and
> a store-hardening milestone for Phase 2 are complete, each approved by an
> independent review; the suite has 1504 tests. It is not on PyPI yet, and a lot is
> deliberately not built -- see [What it does not do yet](#what-it-does-not-do-yet).

## What it does

- **Runs one agent through a tool-calling loop** until it answers, fails, or
  runs out of turns. Runtime failures (a tool that raises, a model that returns
  nonsense, a hook that breaks) end as a status on the result, never as an
  exception in your code. Configuration mistakes still raise, at the call site.
- **Guards every tool call in a fixed order**: validate the arguments against
  the tool's JSON Schema, then check permission, then run it. A call that fails
  either check never reaches your function, and the model is told why.
- **Lets you intercept** model calls and tool calls with runtime hooks that can
  continue, modify or reject.
- **Tags every tool result with provenance**: its origin, trust zone and
  instruction authority, carried as metadata beside the prompt rather than as
  text the model can be talked out of.
- **Talks to any OpenAI-compatible chat completions API** over plain HTTP, with
  retries for timeouts and rate limits and credentials that refuse to appear in
  logs, reprs, stored rows or model context. Verified against OpenAI and
  Anthropic (via Bedrock) models through a LiteLLM gateway.
- **Is honest about how a run ended and what it cost.** A response cut off at
  the output-token limit, or stopped by a content filter, ends the run failed
  with that reason instead of passing half an answer off as complete, and none
  of its tool calls run. Every run reports its token usage, cached and reasoning
  tokens included, and its cost in USD from prices you supply; a model with no
  price reports `None`, never 0. The output limit and reasoning effort can be set
  per agent or per run.
- **Persists runs to PostgreSQL, if you want it**: runs, messages, events and a
  per-run manifest of the exact configuration, every row scoped to a tenant and
  project, reconstructable in order, with each run's usage and cost. Schema
  changes ship as versioned, checksummed migrations. Connections are pooled and
  validated, store calls stay off the event loop, concurrent writers to one run
  all commit, and a run can record the run that spawned it.
- **Offers read-only built-in tools you opt into** (`agentsdk.builtin_tools`):
  read, list, glob and grep confined to a root folder, checked on the file
  actually opened so no link, junction or short name leads out; a fetch tool
  limited to an allowlist and to public internet addresses, sending no
  credentials and ignoring proxy settings; and web search over a backend you
  supply. Fetched and searched content is labelled untrusted. Every tool's
  output is capped, 50,000 characters by default. The file tools are
  Windows-only in this release.
- **Records every page a run fetches as an immutable source version**, its
  content an artifact checked against its hash, and serves a fresh one again
  from a cache instead of refetching: within the project by default, an hour's
  freshness, the scope widened (tenant, everyone) or narrowed (the top-level
  run, no caching) per tool or per host. A hit stored in another tenant or
  project is copied into the reader's own, never shared, and each fetch emits
  `SourceFetched` or `SourceServed` (M22); one that waited on another's fetch
  of the page and was answered with an error status emits `SourceUncached` with
  its wait (M25).
- **Keeps an evidence ledger per top-level run** (`agentsdk.ledger.ledger_tools`):
  agents record claims with `record_claim`, citing the pages they fetched, and
  correct them with `supersede_claim`. One statement, however it is cased or
  spaced, is one claim with every agent's contribution; its sources and labels
  are derived from those contributions, so a claim drawn from untrusted pages
  stays untrusted; nothing is updated or deleted, and a supersession is a chain
  that never forks. Read it with `Runner.evidence_ledger()` (M23).
- **Gates plan nodes on their evidence** (`GatePolicy`, M26): a node carrying a
  `critic` criterion, or every node when the policy says so, is judged after its
  deterministic criteria by rule signals over the claims its child recorded -- a
  claim with no source, a contradicting source, too few sources, a confidence
  below the threshold, or no claim at all -- with no model call. Clean signals
  clear the node; each judgement is an append-only `EvidenceVerification` and a
  `GateEvaluated` event, and an application records externally validated
  outcomes with `Runner.record_validation()`. No verification makes a claim or
  an answer cleaner.
- **Calls a critic only when the signals fire** (M27): the policy's critic, an
  agent with no tools, runs once per node execution as a child of the
  orchestrator on the node's own budget, briefed with the answer, the claims and
  the pages they cite as data. It approves (the node is done), corrects (its
  corrections supersede the claims as its own contributions, and the node takes
  its answer, tainted as both) or escalates (the node fails and is replanned),
  and its verdict is recorded and emitted as `CriticVerdict`.
- **Reports how well the gate is calibrated, and changes nothing** (M28):
  `Runner.calibration_report()` counts, for one tenant and project, each
  signal and each confidence threshold from 0.05 to 0.95 against the external
  outcomes recorded for the claims the gate judged, with the critic's verdicts
  beside them, and suggests the smallest threshold that would have flagged every
  incorrect claim. The suggestion is only reported: the threshold you configured
  stays until you change it.
- **Waits for a person's approval before a call that needs one** (M30): a tool
  that declares `approval_policy=REQUIRE_APPROVAL`, a `needs_approval` predicate
  over a call's arguments, or a hook returning `REQUIRE_APPROVAL` makes the call
  wait. Every approval of a turn is asked at once and recorded in the database;
  an application lists them with `Runner.interruptions()` and decides with
  `Runner.decide()` or the run handle's `approve()`/`reject()`, as a named user or
  service. Calls keep the order the model gave them, the model is not called
  while any waits, a rejection's reason reaches the model, an approval may be
  made sticky for the rest of the run, and an undecided call expires after
  `RunConfig.approval_timeout` (an hour by default). What runs is exactly what
  was approved: the arguments are copied when the request is made.
- **Writes and edits files, only as approved** (M31): `write_file_tool(root)`
  creates or replaces a UTF-8 file and `edit_file_tool(root)` replaces text that
  occurs exactly once, both confined to `root` as the read tools are (Windows
  only). Each needs approval by default and shows the approver the path and the
  file's hash before and after; a file that changed since then is not written. A
  write goes to a hidden temporary file that then replaces the target in one step,
  so a reader sees the old file or the new one, and nothing is left behind.
- **Asks for approvals made anywhere in an orchestrated plan on the top-level
  run** (M32): a child's call that needs approval is recorded against the child,
  announced on the top-level run's event stream with its node, listed by
  `Runner.interruptions(run_id=<top-level>)` and decided through the top-level
  handle, however deep the child. A waiting child keeps its node's budget and its
  concurrency slot while the other nodes run, inherits its parent's
  `approval_timeout`, and is cancelled -- its approvals recorded `cancel` -- with
  the top-level run.
- **Uses the tools of MCP servers natively** (M33): servers configured in code
  (`agentsdk.mcp.StdioServer`) are started over stdio and spoken to in either
  protocol era. Their tools reach an agent as `<alias>__<name>` only when its
  profile names them, after the server's allow and deny lists; a hostile listing's
  dotted, duplicate or clashing names are skipped and recorded; every result is
  untrusted MCP data; each call needs approval unless the server says `never`. A
  connection serves one tenant and project, restarts once, and `Runner.aclose()`
  ends its server. Needs the `agentsdk[mcp]` extra.
- **Runs an MCP tool only in a version someone accepted** (M34): a catalog per
  tenant and project records every version of a tool a connection sees, by a hash
  of its name, title, description, schemas and annotations. A tool seen for the first
  time, or changed, is blocked, and `McpToolBlocked` names what changed;
  `Runner.accept_tools` -- or a server's `accept`, for a service principal --
  makes it usable. The listing is read again on a list-changed notification in
  either era, when its `ttlMs` passes, and once it is 60 seconds old, and a call
  to a tool changed since the model saw it is refused `ToolBlocked`. Approval
  follows the version: a sticky decision applies only to the hash it was made
  for, and a trusted server may ask for approval `by_annotations`.
- **Reaches MCP servers over Streamable HTTP** (M35): `agentsdk.mcp.HttpServer`
  takes an `https` URL, or `http` to loopback, and headers configured in code,
  whose values reach the server and nothing else. Every response passes one
  transport the SDK owns: a redirect, a 401 or 403, and a body over 4 MiB are
  refused, and no URL from a response is ever opened. Resources and prompts are
  the application's: `Runner.mcp_resources` and `mcp_read` (stored as an
  artifact of the tenancy, labelled untrusted, ready to brief a child), and
  `Runner.mcp_prompts` and `mcp_prompt` (an `McpPrompt`, used only once its
  version is accepted). `resources_as_tool=True` offers an agent
  `<alias>__read_resource`, under the server's approval.
- **Lets an MCP server ask a person for more input** (M36): a form to fill in, or
  a URL to open, in either protocol era. The request is recorded as an
  interruption of the run -- with the schema, or the full URL, which the SDK
  never opens -- and the call waits, past its `call_timeout` and up to the run's
  `approval_timeout`, until the application answers with `Runner.respond` (or
  `RunHandle.respond`): `accept` with content matching the schema, `decline` or
  `cancel`. The answer reaches the server and the same call continues under its
  original approval; expiry and the run's cancellation answer `cancel`, a fourth
  request fails the call, and a sampling or roots request is recorded and
  answered unsupported. Events name each request, never the response.
- **Holds against hostile MCP servers under orchestration** (M37, Phase 4's exit
  signal): example 31 runs a plan whose children use five servers at once -- one
  changing a tool mid-run, one poisoning a description, one shadowing another's
  tool name, one injecting instructions in a result, one asking for input -- with
  an approver deciding from the top-level run. `Runner.mcp_blocked` lists the
  tool versions a tenancy's connection blocks now, so a version only a child's
  run met can be accepted from anywhere. An input request whose message, with
  its schema or URL, is over 64 KiB as JSON is refused, recorded nowhere, and
  fails its call `input request too large`.
- **Runs an agent's commands in a workspace, not on the host** (M38, Phase 5's
  first increment): an agent whose spec has a `WorkspaceSpec` is given a
  workspace the first time one of its workspace tools needs it --
  `run_command`, `read_workspace_file`, `write_workspace_file` and
  `list_workspace`, offered only when its profile names them -- through the
  Runner's `workspace_provider`. `run_command` waits for approval by default,
  every result is untrusted executable data, a workspace past
  `max_workspace_bytes` refuses further work by name, and the workspace, each
  command and its removal are recorded on both stores. M38's provider is
  `FakeProvider`, in process, for tests and offline examples; M39 runs commands
  in Docker containers.
- **Fetches each page once, however many agents ask** (M24's claim-lock): one
  holder per page and per statement, across the tasks of a process and the
  processes sharing the database; the others wait, then take the holder's
  version or add to the holder's claim, and their events say how long they
  waited. A holder that fails is retried by one waiter, with backoff, at most
  three times, and no wait or result ever crosses a tenant.
- **Runs a response's tool calls in parallel when the tools say it is safe.** A
  tool that declares `concurrency_safe=True` (the built-in read-only tools do)
  runs beside the other safe calls of the same response, under
  `SchedulerLimits`: at most 4 tool calls of a run at once by default, optional
  limits per tool, and limits on concurrent model calls per model client, shared
  by every run of a `Runner`. Results reach the model in the order it asked for
  them, and a tool that declares nothing still runs one call at a time. The
  file tools have their own thread pool, so a burst of searches cannot hold up
  database calls.
- **Lets you watch and stop a run while it works.** `Runner.start()` returns a
  `RunHandle` at once: iterate `handle.events()` to receive every event as it
  is recorded, in order, read progress with `handle.state()`, await
  `handle.result()`, or call `handle.cancel()`. A cancelled run stops before
  any further model or tool call, lets a store write already started finish,
  gives every unfinished tool call a `ToolCancelled` result, and ends with
  status `cancelled`, its usage and cost recorded; the cost is `None` when a
  model call was cut off, because that call may already be billed.
  Cancelling the task that awaits `Runner.run()` does the same and then raises
  `CancelledError`. Cancellation is cooperative: a tool that ignores it delays
  the end of its run.
- **Works entirely in memory** when you don't pass a database.

## What it does not do yet

Stated plainly, because an SDK that overstates itself costs you a week:

| not built | where it lands |
|---|---|
| Within orchestration (plans run as DAGs of child runs, with deterministic acceptance criteria and replanning, since M19): loop nodes; and any behaviour for a node's `timeout` and `retry_policy`, which are stored but not acted on | Loops in Phase 5A; timeout and retry not yet scheduled |
| The gate judges the claims a node's own child recorded; a run that child spawned itself is outside them (a node's child does not spawn today). A node gated only because the policy gates every node, and that recorded no claim, is cleared | Declared (M26) |
| Every deterministic criterion of a node runs before a critic criterion is judged or refused, so with no critic configured a `tool_succeeds` criterion's side effects happen before the node fails `criterion_not_available` | Declared (M26a) |
| On a database where M26 already recorded a verification naming a claim of another ledger, migration 0013 cannot apply until that row is removed: adding its key checks every existing row, and the migration rolls back whole | Declared (M27) |
| Example 23's taint checks compare each corrected claim and answer with the critic's own rows, but in its corpus the critic is tainted exactly as the pages are, so they cannot tell a missing critic label; `tests/test_critic.py`'s test that taints the critic's result alone is what pins it | Declared (M29) |
| A write tool re-reads its target's hash just before replacing it, but a program outside the SDK can still change the file between that read and the replace; the window cannot be closed from user mode | Declared (M31) |
| A write is refused `target in use` when any other program reading it, or otherwise holding it open, has it open at the moment of the replace -- in M31's review 43 of 60 writes were refused with one reader polling -- so on a machine with editors or indexers it is common; it is not retried, and the model sees the refusal and may try again | Declared (M31, corrected M32) |
| No model can record an externally validated outcome: `Runner.record_validation` is in no tool registry and no tool the SDK ships calls it. A custom tool is first-party code in your process and reaches the Runner through its scope, as it reaches the ledger store, so it could call `record_validation`; in-process code is outside the threat model | Declared (M26a) |
| Streaming of model tokens as they are generated (a run's events do stream, through `RunHandle.events()`) | Later; not yet scheduled |
| Native MCP over Streamable HTTP, MCP resources and prompts, and a server's requests for input | Phase 4 (M35, M36) |
| A tool's hash covers its name as well as the description, schemas and annotations FR-127 names, so two tools of one server with the same description and schemas never share one, and accepting one never accepts the other | Declared (M34) |
| A hash covers every field a person or a model can read -- a tool's title too, a prompt's arguments, a template's URI template and type -- and never `_meta`, `icons` or `execution`, which the SDK never reads, shows or fetches; the `mcp` library drops annotation keys it does not know before the SDK sees them (owner's choice, M35) | Declared (M35) |
| A schema the SDK could not check within its 10 s is refused, and that verdict is kept for the connection's life, so a listing read again does not check it again: a check that timed out on a slow host keeps that tool skipped until the schema changes or the server starts again (owner's choice, M35) | Declared (M35) |
| A 2026-07-28 server that does not offer `subscriptions/listen` is not asked again that session, and its listings are kept for its `ttlMs` or 60 seconds, as a legacy server's are; a listing's age runs from the start of its read | Declared (M35) |
| An `HttpServer` URL holding a query, a fragment or user information is refused when the server is built: a credential belongs in `headers` (owner's choice, M35). A header the SDK or the library sets itself -- `Content-Type`, `Accept`, `Host`, `MCP-Protocol-Version`, `Mcp-Session-Id` and the like -- cannot be configured | Declared (M35) |
| A header value must be visible ASCII, with spaces or tabs only inside it: one the HTTP library would refuse is refused when the server is built, since the library quotes a refused value whole in its debug log | Declared (M35) |
| The SDK asks for unencoded responses and refuses any other, `EncodingRefused` at connection: a compressed body would be inflated after it was counted, past the 4 MiB bound | Declared (M35) |
| A connection's 10000-version limit is shared by its tools, prompts and resource templates, and counts entries, not bytes: a server that changes its prompts without end, read by `mcp_prompts` again and again, can use it up, and that connection's later tool versions stay blocked, naming `VersionLimit` | Declared (M35) |
| An `HttpServer` URL's path is recorded in the manifest as written: a credential in the path, which no rule can tell from any other path, is recorded with it. A long-lived event stream -- the 2026-07-28 `subscriptions/listen` -- is ended once it carries 4 MiB, and reopened at the next read | Declared (M35) |
| A response body over 4 MiB is refused as it arrives, before it is parsed; on an event stream the `mcp` library refuses one event over 1 MiB first. Either fails that call, and the connection stays usable. A response the transport refuses at connection leaves the server unavailable, named `RefusedAuthorization`, `RedirectRefused` or `ResponseTooLarge` | Declared (M35) |
| An HTTP server whose transport fails -- the server stopped, say -- has that session ended, and is started again under the restart-once rule, as a stdio server that exits is | Declared (M35) |
| Resource and prompt listings are read again at every `mcp_resources`, `mcp_prompts` and `mcp_prompt` call; a prompt accepted at its listing is fetched by `get_prompt`, whose messages are not part of the hash, so a server can change what a prompt says without changing its version | Declared (M35) |
| A resource template's acceptance gates its listing only: `mcp_read` and `<alias>__read_resource` read any URI the application, or an approved call, chooses, and what they return is untrusted either way (owner's choice, M35) | Declared (M35) |
| With `resources_as_tool`, the SDK's `<alias>__read_resource` wins over a server tool of that name, which is skipped and recorded; it is approved by `approval_overrides["read_resource"]` when set, else by the server's `approval`, needs no acceptance (its description is the SDK's) (owner's choice, M35); it is not filtered by `allow` or `deny`, which name the server's own tools | Declared (M35) |
| A server that sends a configured header's value back in its own output -- a result, a prompt, a resource -- has it stored as that output; the SDK never sends it anywhere else | Declared (M35) |
| A 2026-07-28 server that changes its tools between a listing read and its `subscriptions/listen` stream opening, and says so in that window, is caught at the next read: within 60 seconds | Declared (M36) |
| A server may ask for a secret in a form, though the specification forbids it, and the SDK cannot tell one: an accepted form's content is stored with the decision, labelled user-controlled data. A url-mode URL may carry a token, and is stored whole, since the person must see it whole | Declared (M36) |
| An input request is recorded on the call it belongs to. On 2026-07-28, and on the legacy era over HTTP when the server sends it on the call's own response stream, that call is known exactly. A legacy-era request that names no call -- over stdio, or on the HTTP standalone stream -- is attributed only when one call is in flight on the connection, possibly another run's, and otherwise answered `cancel`, each call in flight told by `InputUnattributed` (owner's choice, M36). An application read's request is answered `cancel`, and no run is told | Declared (M36) |
| When a call that waits for input is cancelled, the `mcp` library's Streamable HTTP client may log an "Error parsing SSE message" traceback for the stream it abandons; nothing is lost | Declared (M36) |
| A fourth input request of one call fails it `too many input requests`: on 2026-07-28 the requests of that round still waiting are decided `cancel`, and on the legacy era the server's tool may finish its work before the call fails. Sampling and roots requests count toward the three | Declared (M36) |
| An input request made during an application read -- `mcp_resources`, `mcp_read`, `mcp_prompts`, `mcp_prompt` -- has no run to record it on, and is answered `cancel`; one made during a `<alias>__read_resource` call is recorded on its run as any call's is. An accepted form's content must be the protocol's answer -- text keys; text, number, boolean, null or a list of text -- of at most 64 KiB, or `respond` refuses it | Declared (M36) |
| A cancelled run's input requests are decided `cancel` before the run reports `cancelled` (FR-50): a cancelled call waits for its work on the MCP loop to end, for at most 40 s. If the store hangs past that, the run reports `cancelled` first and the decision is written when the store answers -- bounded best effort (M37) | Declared (M37) |
| The 64 KiB cap is per request: one call may make three requests, so one 2026-07-28 round, or one call, may store up to three requests of 64 KiB (M37) | Declared (M37) |
| An input request's size is measured as JSON with non-ASCII characters escaped, so a request in another script is refused sooner than its stored size suggests: strict, never a bypass. Once one request of a call is refused -- too large, or past three -- every later request of that call is refused unrecorded, and the call fails for the first reason (M37) | Declared (M37) |
| `Runner.mcp_blocked` reads through the tenancy's connection, as `mcp_resources` does: it starts a server not yet started, and a restart it causes counts toward the server's one restart. It lists the tools the server lists now, never one the SDK skipped as unusable (M37) | Declared (M37) |
| Live, examples 30 and 31 report a check whose subject the model never brought about as `[SKIP] not exercised`, naming why. In example 31 a subject is the least the servers' own journals show happened -- a server ran anything, a call that asks ran, the server asked -- so a violation fails rather than skips; the secrets check is asked of every run. Each server returns an id for every execution, which tells the execution to exactly one call: nothing is matched by arguments, and an error raised before a call left is never counted as a server's. An execution whose result never reached a call -- a cancelled plan's -- fails the approval check live. The full witnesses a scripted run must show are asked offline only. Every `[FAIL]` names the clause that failed (M37 rounds 2 and 3) | Declared (M37) |
| Example 31's checks read a child's events and manifest only on PostgreSQL; the in-memory store keeps no child's events, so there they read the plan run's events, the histories, the interruptions and the catalog's answers | Declared (M37) |
| Workspaces run only on `FakeProvider` until M39 brings `DockerProvider`: its commands are Python functions the application registers, and it isolates nothing from the host process. It is for tests and offline examples | Declared (M38) |
| A workspace's disk bound is measured after each command and write, not enforced while they run: the one that passes `max_workspace_bytes` completes, and every later one is refused until files are removed (P5-D7) | Declared (M38) |
| `WorkspaceSpec(scope="plan")` and `keep=True` are accepted by the spec and refused when a run starts, until M41; `max_concurrent_workspaces` is checked and not yet enforced (M41) | Declared (M38) |
| A workspace is removed when its run ends, before the run's last event. `Runner.aclose()` does not yet remove the workspaces of runs still under way, and nothing yet reaps a workspace a crashed process left (M41) | Declared (M38) |
| Beside a `workspace_provider`, no registered tool may be named `run_command`, `read_workspace_file`, `write_workspace_file`, `list_workspace` or `export_workspace_file`: it would run on the host, so such a Runner is refused when it is built | Declared (M38) |
| A `WorkspaceSpec.env` value reaches nothing the SDK records but the output of a command that prints it -- and, when a model copies it into a later command, that command's arguments, recorded as written in its event and row as every argument is in the run's messages (NFR-45 as amended, DECISION-bb81ebbe) | Declared (M38) |
| A command cut short by its run's cancellation is recorded `killed: cancelled`, with no output: what it printed before the cancellation is not kept | Declared (M38) |
| Example 31's live "lost executions" clause counts per qualified tool: a server's journal names no run, so an execution whose result reached no call is matched only by a call of that tool cut short by a cancellation or a timeout (DECISION-97fb116b) | Declared (M38) |
| While a call waits for a person, the `mcp` library's own read timeout for a legacy-era call is paused through its request clock, a private part of mcp 2.3.0 that the extra pins; a later library may move it | Declared (M36) |
| A server that changes a tool without saying so is caught at the next read of its listing: once its `ttlMs` passes, once it is 60 seconds old, or before a call when either has passed. Its results are untrusted either way | Declared (M34) |
| A 2026-07-28 server that sends `ttlMs` 0 -- the mcp library's own servers do by default -- has its listing read again at every turn, and before every call both at executor step 1 and as the call leaves, a round trip each, one read at a time per connection; its schemas are checked again only when they change. The legacy era carries no `ttlMs`, so its listings are kept 60 seconds unless the server says they changed | Declared (M34) |
| A listing that cannot be read again ends that session: the server's tools are offered to no turn, and a call already offered one is refused at executor step 1, before its arguments are validated or its approval asked, until the server starts again under the restart-once rule -- at the next check before a call, or the next turn | Declared (M34) |
| A Runner's connection for one tenant and project records at most 10000 versions of its server's tools, across restarts; a tool whose version would pass that stays blocked for that Runner's life, its `McpToolBlocked` naming `VersionLimit`. The versions it did record stay in the catalog | Declared (M34) |
| A read again that adds or removes the tool an `approval_overrides` key names changes every tool's approval with no event of its own; a tool object a turn was offered before it is refused `ToolBlocked`, and the next turn offers the tools as they are now | Declared (M34) |
| An acceptance is never withdrawn: once a tenancy accepts a tool's version, that version stays usable there; a change is a new version, blocked until accepted | Declared (M34) |
| While a server's `approval_overrides` names a tool it does not list -- a typo, or a tool since removed -- every tool of that server asks for approval, and `McpServerConnected` names the unmatched keys (owner's choice, M34) | Declared (M34) |
| A stdio server whose command is a launcher -- a Windows virtual environment's `python.exe`, a `.cmd` shim -- may start the real server before the `mcp` library's Job Object holds the launcher, so that server's children can outlive `Runner.aclose()`; point `command` at the real executable | Declared (M33) |
| A stdio server inherits the `mcp` library's environment -- on Windows APPDATA, HOMEDRIVE, HOMEPATH, LOCALAPPDATA, PATH, PATHEXT, PROCESSOR_ARCHITECTURE, SYSTEMDRIVE, SYSTEMROOT, TEMP, USERNAME and USERPROFILE, but not TMP -- with its `env` added on top | Declared (M33) |
| The `mcp` library validates a server's whole tool listing, so an entry it refuses refuses them all and the server is reported unavailable: an input schema whose type is not `object`, or whose `$schema` is not text, a schema nested more than about 60 levels deep, and a lone surrogate in a name or key (the library drops that message, so the start waits out `connect_timeout`). Entries the SDK itself refuses are skipped one tool at a time. The SDK replaces the library's own check of structured results against a tool's output schema with its isolated one, below | Declared (M33) |
| No schema a server supplies is evaluated in the SDK's process: the model's arguments, the server's results and its listing are each checked in a short-lived process of their own, ended after 10 s, resolving references only within the schema; a listing's input and output schemas are checked 50 tools to a process, side by side, up to twenty at once, and a part that cannot be checked in time has all its tools skipped, so a hostile tool takes its chunk's neighbours with it; on a host with fewer processors than chunks a large honest listing takes longer and may be cut so. Validation can take time exponential in what a server chose (a regular expression, which also holds the interpreter lock, or deep `anyOf` nesting with none), so the check is isolated and bounded. That costs about 0.15 s per check | Declared (M33) |
| A server's data is bounded by deadlines and memory, not by size: a message the `mcp` library cannot parse is waited out to its timeout, a very long line is read in time that grows with its square, and tool descriptions are not capped | Declared (M33) |
| A Runner given MCP servers runs them on one event loop of its own, on a background thread, whichever loop its runs are on; `Runner.aclose()` ends that thread with the servers | Declared (M33) |
| A server that cannot be reached is retried once a minute per tenant and project, so N tenancies of one Runner each wait one `connect_timeout` a minute on a server that is down | Declared (M33) |
| A server that writes something other than MCP to its stdout -- its environment, say -- has that line quoted in the `mcp` library's own parse-error log record (`mcp.client.stdio`), which the SDK does not silence | Declared (M33) |
| A paused run resuming after its process restarts | Phase 6 |
| Sandboxed execution: commands in a Docker container (M39); timeouts and cancellation that kill a command's container (M40); files out of a workspace as artifacts, plan workspaces, kept workspaces and reaping (M41) | Phase 5 |
| Pause and resume; durable interruptions (cancel is built) | Phase 6 |
| The execution manifest's `tools_sent` records the tools a run's agent is sent, written at run start with the rest of its configuration, so a run that ends before its first request records a set it never sent | Declared (M21) |
| A fetch tool widened to a broader cache scope keeps serving the run's own version recorded at the narrower one until that version expires | Declared (M23) |
| A claim's labels count the run's tool results already in its history when `record_claim` is called; results of other calls in the same parallel batch are not yet there and do not count | Declared (M23) |
| A holder that fails is retried by one waiter in its own process; waiters in other processes on the same database each take their turn and retry for themselves. A hung holder keeps its key until its tool times out; leases with expiry are Phase 6's | Declared (M24) |
| After a failure a key has three attempts in all while any call is on it; a success in between does not restore them, so the key is fresh again only once no call is left on it | Declared (M25) |
| A claim-lock call cancelled while a worker thread takes or releases its database lock waits for that thread however long it runs, as every store call does; a thread that never returns keeps the call | Declared (M25) |
| A claim-lock holder keeps its pooled connection while its own operation uses the same pool, so 32 concurrent holders can exhaust it | Declared (M25); Phase 6's leases |
| A fetch that waited on another's fetch and then raised (refused, timed out, failed) records no wait; one answered with any status records it | Declared (M25) |
| Since M25 the ledger store's transaction locks and the manifest's `agent_spec_hash` (now `v2:` and a hex digest) are encoded so no two names collide; processes writing one database must all run on the same side of M25, and a hash written before it is never compared with one written after | Declared (M25) |
| A source version's content is the fetch tool's whole result, status line included, so a served page reads exactly as it did when fetched; a copy made into another tenant or project keeps the original's final URL and media type with its hash and retrieval time | Declared (M22) |
| A session store's combined write (a compaction's summary and its `ContextCompacted` event) refuses an event sink that names another run, tenant or project. The Runner always passes the run's own sink; only code driving `AgentLoop` itself with another run's sink meets the refusal | Declared (M21a) |
| On PostgreSQL, a custom event sink that names no scope, or that cannot write inside the store's transaction (it lacks `write` and `stored`), gets the summary and its event as two writes, so if its `emit` fails the summary stays stored without its event; the SDK's own sinks write both in one transaction | Declared (M20a, M21a) |
| Under a `ContextPolicy` an agent is sent its `tool_profile`'s tools even when its own `permission_policy` permits others; a call to one of those is refused as an unknown tool | Phase 4's policy engine, which can list what a checker permits |
| Context compaction for a run without a `ContextPolicy`: a plain run still sends its full history every turn. A run carrying one, as every orchestrated run does since M20, compacts at 0.75 of its context window | Opt in with `RunConfig.context_policy` |
| Budget enforcement (cost is measured and recorded, never limited); retention and partitioning of stored rows | Phase 2 / Phase 8 |
| A second wire format (for example Anthropic's native Messages API) | Deferred by choice; the contract was checked against it |
| Enforcing structured output | Phase 2. The slot exists and is unused |

## Requirements

- Python 3.11 (the version everything was tested on)
- An OpenAI-compatible API endpoint and key
- PostgreSQL 16, only for persistence and the full test suite

## Install

```bash
git clone https://github.com/siva-rgb/Custom-Agent.git
cd Custom-Agent
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt     # macOS / Linux
```

Or install it as a package, together with what the examples need:

```bash
.venv/Scripts/python -m pip install -e ".[examples]"
```

The runtime dependencies are `httpx`, `jsonschema`, `psycopg[binary]`,
`psycopg_pool` and `python-dotenv`. No vendor agent SDK is required, and none is
allowed.

## Configure

```bash
cp .env.example .env
```

| variable | what it is |
|---|---|
| `BASE_URL` | root of the API, **without** `/v1` -- the client calls `{BASE_URL}/v1/chat/completions` |
| `MODEL_API_KEY` | bearer token for that API |
| `DATABASE_URL` | `postgresql://user:password@host:5432/dbname`, only for persistence |
| `DEFAULT_MODEL` | model id used when an agent names none (default `openai.gpt-4o-mini`) |

## Quickstart: an agent with a tool

Save as `quickstart.py` in the repository root and run it.

```python
import asyncio

from agentsdk import AgentSpec, RunConfig, Runner
from agentsdk.config import Settings
from agentsdk.providers import OpenAICompatibleModelClient
from agentsdk.tools import Tool, ToolSpec


def add(a: int, b: int) -> str:
    return str(a + b)


add_tool = Tool(
    spec=ToolSpec(
        name="add",
        description="Add two whole numbers.",
        input_schema={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    ),
    fn=add,
)


async def main() -> None:
    settings = Settings.from_env()  # BASE_URL, MODEL_API_KEY, DEFAULT_MODEL from .env
    client = OpenAICompatibleModelClient(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.default_model,
    )
    runner = Runner({"gw": client}, tools=[add_tool])
    agent = AgentSpec(
        id="calculator",
        instructions="Use the add tool for arithmetic.",
        preferred_model=f"gw:{settings.default_model}",
        tool_profile=("add",),  # the tools this agent is allowed to call
    )
    try:
        result = await runner.run(
            agent, "What is 17 + 25?", RunConfig(tenant_id="demo-tenant", project_id="demo")
        )
    finally:
        await client.aclose()

    print(result.status.value, "|", result.output)
    print("tokens used:", result.usage.total_tokens)


asyncio.run(main())
```

`tool_profile` is an allowlist, and an empty one allows nothing. A tool that is
registered but not in the profile is refused, and the model receives the refusal
as an error result it can respond to. By default the model is still sent every
registered tool's schema; a run with a `ContextPolicy` (`RunConfig.context_policy`,
which every orchestrated run carries) is sent only the tools its profile allows, and a
call to any other is refused as an unknown tool is.

## Test an agent without a network

Anything with `async def send(request) -> ModelResponse` is a model client, so a
scripted one makes agent logic testable offline and deterministically.

```python
import asyncio

from agentsdk import AgentSpec, Message, Role, RunConfig, Runner, RunStatus, ToolCall
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.tools import Tool, ToolSpec


class ScriptedModel:
    async def send(self, request):
        tool_results = [r for m in request.messages for r in m.tool_results]
        if not tool_results:
            call = ToolCall(id="call-1", name="shout", arguments={"text": "hello"})
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, tool_calls=(call,)),
                stop_reason=StopReason.TOOL_CALLS,
                usage=Usage(10, 5, 15),
            )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content=f"The tool said {tool_results[-1].content}"),
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 5, 15),
        )


shout = Tool(
    spec=ToolSpec(
        name="shout",
        description="Upper-case some text.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    ),
    fn=lambda text: text.upper(),
)


async def main() -> None:
    runner = Runner({"scripted": ScriptedModel()}, tools=[shout])
    agent = AgentSpec(id="shouter", instructions="Shout.", tool_profile=("shout",))
    result = await runner.run(
        agent, "Say hello loudly", RunConfig(tenant_id="demo-tenant", project_id="demo")
    )

    assert result.status is RunStatus.COMPLETED
    assert result.output == "The tool said HELLO"
    print([event.event_type.value for event in result.events])


asyncio.run(main())
```

## Intercept tool calls with a hook

Permission decides whether an agent *may* call a tool; a hook can still refuse a
particular call. Here `delete_file` is in the profile, so the allowlist passes
it, and the hook rejects it.

```python
import asyncio

from agentsdk import AgentSpec, Message, Role, RunConfig, Runner, ToolCall
from agentsdk.hooks import HookAction, HookOutcome, RuntimeHook
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.tools import Tool, ToolSpec


class FrozenDeletes(RuntimeHook):
    def before_tool(self, tool_call):
        if tool_call.name == "delete_file":
            return HookOutcome(action=HookAction.REJECT, reason="deletes are frozen today")
        return super().before_tool(tool_call)


class TriesToDelete:
    async def send(self, request):
        if not any(m.role is Role.TOOL for m in request.messages):
            call = ToolCall(id="call-1", name="delete_file", arguments={"path": "report.txt"})
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, tool_calls=(call,)),
                stop_reason=StopReason.TOOL_CALLS,
                usage=Usage(1, 1, 2),
            )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="Understood, I left the file alone."),
            stop_reason=StopReason.END_TURN,
            usage=Usage(1, 1, 2),
        )


deleted = []
delete_file = Tool(
    spec=ToolSpec(
        name="delete_file",
        description="Delete a file.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    fn=lambda path: deleted.append(path) or "deleted",
)


async def main() -> None:
    runner = Runner({"m": TriesToDelete()}, tools=[delete_file], hook=FrozenDeletes())
    agent = AgentSpec(id="cleaner", instructions="Tidy up.", tool_profile=("delete_file",))
    result = await runner.run(agent, "Delete report.txt", RunConfig(tenant_id="demo-tenant", project_id="demo"))

    tool_events = [e.payload for e in result.events if e.event_type.value == "ToolCalled"]
    print(tool_events)  # is_error True, error_type ToolPermissionDenied
    assert deleted == [], "the tool must never have run"
    print(result.output)


asyncio.run(main())
```

## Persist and reconstruct a run

Build `Persistence` once, at process start and outside the event loop: it
creates or migrates the schema with blocking DDL.

```python
import asyncio
import os

from dotenv import load_dotenv

from agentsdk import AgentSpec, Message, Persistence, Role, RunConfig, Runner
from agentsdk.config import normalise_database_url
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import PostgresTrace, RunScope


class OneLiner:
    async def send(self, request):
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="recorded"),
            stop_reason=StopReason.END_TURN,
            usage=Usage(1, 1, 2),
        )


load_dotenv()
dsn = normalise_database_url(os.environ["DATABASE_URL"])
persistence = Persistence.postgres(dsn)  # once per process
config = RunConfig(tenant_id="demo-tenant", project_id="demo")


async def main():
    runner = Runner({"m": OneLiner()}, persistence=persistence)
    return await runner.run(AgentSpec(id="recorder", instructions="Reply."), "hi", config)


result = asyncio.run(main())
trace = PostgresTrace(dsn).reconstruct(
    RunScope(run_id=result.run_id, tenant_id=config.tenant_id, project_id=config.project_id)
)
print(trace["run"]["status"], "|", len(trace["messages"]), "messages |", len(trace["events"]), "events")
print("configuration manifest recorded:", trace["manifest"] is not None)
```

## Timings and telemetry

Every `ModelCalled` and `ToolCalled` event records when the call started, its
`duration_ms`, and `queued_ms`, the time it waited for a provider or tool slot. Every
terminal event records the whole run's `started_at` and `duration_ms`. With Postgres
persistence that is already a dashboard, and no exporter is needed:

```sql
-- The slowest model and tool calls of the last day, per tenant and project.
SELECT tenant_id,
       project_id,
       event_type,
       COALESCE(payload->>'model', payload->>'name')     AS called,
       count(*)                                          AS calls,
       round(avg((payload->>'duration_ms')::numeric), 1) AS avg_ms,
       round(max((payload->>'duration_ms')::numeric), 1) AS max_ms,
       round(avg((payload->>'queued_ms')::numeric), 1)   AS avg_queued_ms
FROM run_events
WHERE event_type IN ('ModelCalled', 'ToolCalled')
  AND payload ? 'duration_ms'
  AND "timestamp" > now() - interval '1 day'
GROUP BY tenant_id, project_id, event_type, called
ORDER BY max_ms DESC;
```

To see a run as a trace instead, install the `otel` extra and hand the run to the
exporter. It follows the run from a thread of its own, so a slow or unreachable
collector never slows or changes the run:

```python
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from agentsdk.telemetry import OpenTelemetryExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # OTEL_EXPORTER_OTLP_ENDPOINT
exporter = OpenTelemetryExporter(provider)

handle = await runner.start(agent, "What is 2 + 2?", config)
exporter.export(handle)
result = await handle.result()
```

Each run becomes an `invoke_agent` span, with a `chat` span per model call and an
`execute_tool` span per tool call beneath it, carrying token counts, and a cost when
it is known. No message content, tool argument or tool result is exported.
[`scripts/13_telemetry.py`](scripts/13_telemetry.py) shows it end to end.

## Examples

[`scripts/`](scripts/) holds runnable examples, one activity each. Every one runs
live, or with `--offline` using a scripted model with no network and no
credentials -- which is also how the test suite checks them.

| script | what it shows |
|---|---|
| [`01_minimal_agent.py`](scripts/01_minimal_agent.py) | a tool, an agent, a run, a result |
| [`02_custom_tools.py`](scripts/02_custom_tools.py) | JSON Schema validation, async tools, timeouts, a tool that raises |
| [`03_permissions_and_hooks.py`](scripts/03_permissions_and_hooks.py) | a custom permission checker; hooks that rewrite calls and redact results |
| [`04_persistence_and_trace.py`](scripts/04_persistence_and_trace.py) | PostgreSQL persistence, trace reconstruction, tenant isolation |
| [`05_switching_models.py`](scripts/05_switching_models.py) | several model clients, a preferred model, a per-run override |
| [`06_mcp_tools.py`](scripts/06_mcp_tools.py) | tools from an MCP server, bridged by hand, results marked untrusted |
| [`07_delegating_to_a_child_run.py`](scripts/07_delegating_to_a_child_run.py) | a child run that records its parent |
| [`08_testing_agents_offline.py`](scripts/08_testing_agents_offline.py) | behavioural checks against a scripted model, then a real one |
| [`09_limits_and_cost.py`](scripts/09_limits_and_cost.py) | an output limit that fails a cut-off run honestly, and what a run cost from prices you supply |
| [`10_builtin_tools.py`](scripts/10_builtin_tools.py) | built-in file tools confined to a folder, and a fetch tool confined to an allowlist and the public internet |
| [`11_run_handle.py`](scripts/11_run_handle.py) | a run's events streamed through its handle as they happen, and a second run cancelled mid-flight |
| [`12_artifacts.py`](scripts/12_artifacts.py) | an artifact put, read back and checked against its hash, kept within its tenant, expired and deleted |
| [`13_telemetry.py`](scripts/13_telemetry.py) | per-call timings on every event, and the same run as an OpenTelemetry span tree sent over OTLP/HTTP |
| [`14_plan.py`](scripts/14_plan.py) | a planner's plan checked and stored as an immutable version, its nodes moved with events, and a replan that leaves version 1 untouched |
| [`15_budget.py`](scripts/15_budget.py) | a run ceiling split over a plan, a node's unspent reservation reclaimed, and a run refused its next call with budget_exceeded |
| [`16_subagents.py`](scripts/16_subagents.py) | children fanned out from one parent under its limits, a tainted input staying tainted across the run boundary, and a spawn refused at depth 3 |
| [`17_orchestrator.py`](scripts/17_orchestrator.py) | a four-node plan whose check fails its acceptance criterion, the replan as a second version, and the side-effecting node carried rather than run again |
| [`18_context.py`](scripts/18_context.py) | an agent sent only the one tool its profile names, a long run compacting past 0.75 of its window, and the artifact holding what was replaced, read back against its hash |
| [`19_sources.py`](scripts/19_sources.py) | a page fetched once and then served from the cache in the same run and the next, a refetch after freshness recording a new version that names the old one, and a second tenant getting no hit |
| [`20_ledger.py`](scripts/20_ledger.py) | two subagents recording one statement as one claim with both contributions and both pages cited, the claim untrusted because the pages are, and a wrong claim superseded with the chain read back |
| [`21_contention.py`](scripts/21_contention.py) | Phase 3's exit signal: eight orchestrated readers on four overlapping pages, each page fetched once while the rest wait on the claim-lock, one claim per statement with every contributor, a correction, and a second tenant served nothing |
| [`22_critic.py`](scripts/22_critic.py) | the confidence gate and the critic: a node cleared on clean signals with no model call, one the critic approves, one it corrects by superseding the claim, and one it escalates into a replan |
| [`23_gate_contention.py`](scripts/23_gate_contention.py) | Phase 3's second exit signal: example 21's eight readers with every node gated, the critic run for exactly the nodes the corpus makes doubtful, two critics racing to correct one claim with the loser replanned, then external outcomes and the calibration report |
| [`27_mcp.py`](scripts/27_mcp.py) | an agent using an MCP server's tools natively: qualified names, an allow list, a failing tool, every result untrusted, and the server named in the manifest |
| [`28_mcp_catalog.py`](scripts/28_mcp_catalog.py) | the MCP catalog: each tool's first sight blocked then accepted, a changed description blocked and named, a sticky approval that does not carry to the new version, and the call asked again |
| [`29_mcp_http.py`](scripts/29_mcp_http.py) | MCP over Streamable HTTP: a header the server requires, a prompt refused until accepted and then used as a task, a resource read into an untrusted artifact, and a child briefed with it |
| [`30_mcp_input.py`](scripts/30_mcp_input.py) | MCP input requests: a server's form answered by a scripted person and its URL declined, each recorded as an interruption, the answers reaching the server, and the URL never opened |
| [`31_mcp_contention.py`](scripts/31_mcp_contention.py) | Phase 4's exit signal: five hostile MCP servers under orchestration, over stdio and HTTP in both eras -- a mid-run change accepted through `mcp_blocked`, a poisoned description, a shadowed name, an injected result, a form answered -- checked against each server's journal of its executions, the store, and the server processes |
| [`32_workspace.py`](scripts/32_workspace.py) | A workspace on `FakeProvider`: a command's output, a non-zero exit, `run_command` waiting for an operator's approval, a workspace past `max_workspace_bytes` refusing the next command, every result untrusted, and the workspace and its commands recorded |

```bash
python scripts/02_custom_tools.py --offline
```

Delegation is recorded by hand in those examples, and example 06 bridges MCP
by hand to show the mechanism; the SDK now supports MCP natively (example 27). See
[`scripts/README.md`](scripts/README.md).

## The public API

Driving a run needs only the package root: `Runner`, `AgentSpec`, `RunConfig`,
`RunResult`, `RunStatus`, `ReasoningEffort` and `Persistence`, plus the
primitives (`Message`, `Role`, `ToolCall`, `ToolResult`, `ContentProvenance` and
its enums) and the error types.

Defining tools, model clients and prices currently means importing from
submodules: `agentsdk.tools` (`Tool`, `ToolSpec`), `agentsdk.providers`
(`OpenAICompatibleModelClient`), `agentsdk.registry` (`ModelRegistry`,
`ModelPricing`), `agentsdk.hooks`, `agentsdk.model` and `agentsdk.postgres`.
Narrowing that is a known, recorded gap.

## Run the tests

```bash
.venv/Scripts/python -m pytest -q
```

The full suite (1504 tests) runs against a **real database and the live gateway**,
including an evaluation that calls two real models and spends tokens. Tests that
need configuration fail rather than skip when it is missing, on purpose: a test
suite that skips to green proves nothing.

## How it was built

Specification first, with every change gated. [`SPEC.md`](SPEC.md) holds the
requirements and acceptance criteria. `.genesis/` holds the decisions, recorded
knowledge, test evidence and the verdicts of independent reviews, each run in a
separate session that did not write the code. Milestones were approved only
after those reviews passed: the persistence milestone took nine rounds, the live
evaluation three, and the store hardening three.

## Project layout

```
agentsdk/
  api.py            Runner, AgentSpec, RunConfig, RunResult
  loop.py           the agent loop
  executor.py       validate -> permission -> hook -> execute -> provenance
  tools.py          Tool, ToolSpec, ToolRegistry
  permissions.py    permission checkers
  hooks.py          runtime hooks
  model.py          ModelRequest, ModelResponse, Usage, the ModelClient protocol
  registry.py       ModelRegistry, ModelPricing and cost
  providers/        the OpenAI-compatible HTTP client
  workspace/        WorkspaceSpec, the provider protocol, FakeProvider, the workspace tools
  primitives.py     Message, ToolCall, ToolResult, ContentProvenance
  persistence.py    Persistence
  postgres.py       Postgres stores, pool and trace
  migrate.py        versioned migrations
  schema.sql        the baseline schema
  migrations/       numbered schema changes
scripts/            runnable examples, live or --offline
tests/              the test suite
SPEC.md             the specification
.genesis/           decisions, knowledge, evidence and review verdicts
```

## License

[MIT](LICENSE)
