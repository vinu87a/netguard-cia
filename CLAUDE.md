# NetGuard-CIA — Network Change-Impact Analysis (Claude Code project brief)

> Read this file first. It orients you (Claude Code) to what this project is, how
> the pieces fit, and where to find detail. Every referenced file lives in this
> folder tree.

## What we are building

A chat application where a network engineer uploads device configuration files
(Cisco IOS, Juniper Junos), then asks **plain-English "what-if" questions**
("what breaks if I shut the AS1–AS3 link?"). The system answers with a
**Go / No-Go verdict** grounded in deterministic analysis, not LLM guesswork.

The hard rule that shapes every design decision:

**Batfish computes the facts. The LLM translates intent and synthesizes the
verdict. The LLM never reasons about routing/reachability on its own.**

Batfish is deterministic for session status, RIB, best-path, reachability,
differential reachability, and filter behavior. For all of that the LLM's job is
to (a) pick the right Batfish tool and frame the query, and (b) turn the
structured result into a verdict. Anything the LLM asserts about the network that
did NOT come from a Batfish tool is a bug.

## Architecture (three layers)

```
┌────────────────────────────────────────┐
│  Streamlit chat UI  (app/)             │  user uploads configs, asks scenarios
└───────────────┬────────────────────────┘
                │  LLM (translator + synthesizer)  — prompts in prompts/
┌───────────────▼────────────────────────┐
│  Batfish MCP container                  │  ~50 tools; we use ~12 (Misc/reference/)
│  (Presidio-Federal/batfish-mcp-container)│
└───────────────┬────────────────────────┘
                │  Batfish API
┌───────────────▼────────────────────────┐
│  Batfish all-in-one engine container    │  the deterministic analysis engine
└────────────────────────────────────────┘
```

## The flow (four conceptual boxes, one LLM)

1. **Upload** — user drops configs → LLM stages + inits a Batfish snapshot →
   checks parse status → surfaces any parse failures before proceeding.
2. **Translate** — user's English scenario → LLM selects Batfish tool(s) and
   frames the query (and, for edit scenarios, builds the change snapshot).
3. **Compute** — Batfish runs; returns structured results. No LLM reasoning here.
4. **Synthesize** — LLM emits a two-zone answer: **FINDINGS** (engine facts only)
   then **VERDICT** (Go/No-Go with every judgment step tagged).

Full detail: `Misc/docs/02_flow_spec.md`.

## Where to find things

- `Misc/docs/01_architecture.md` — components, data flow, why it's shaped this way.
- `Misc/docs/02_flow_spec.md` — the turn-by-turn flow, upload turn vs scenario turn.
- `Misc/docs/03_tool_inventory.md` — the REAL Batfish MCP tool list (from source),
  which ~12 we use, which ~30 we hide, and the two schema unknowns to confirm.
- `Misc/docs/04_snapshot_stacking.md` — how stacked scenarios work WITHOUT a
  fork_snapshot tool (the ledger + re-stage approach).
- `Misc/docs/05_output_contract.md` — the FINDINGS/VERDICT two-zone format and the
  provenance tags.
- `Misc/docs/06_build_plan.md` — ordered implementation steps for you (Claude Code).
- `prompts/translator_system_prompt.md` — the LLM's front-half prompt.
- `prompts/synthesizer_system_prompt.md` — the LLM's back-half (verdict) prompt.
- `prompts/vendor_reference.md` — Cisco vs Junos behavior grounding.
- `docker/docker-compose.yml` — the two-container stack.
- `docker/README_docker.md` — bring-up, ports, health checks, gotchas.
- `app/streamlit_app.py` — the chat UI shell (skeleton, wired to MCP client).
- `app/mcp_client.py` — thin MCP client wrapper (skeleton).
- `Misc/reference/tools_init.py` — the upstream tool package __init__ (ground truth
  for tool names).

## Non-negotiables (do not violate when implementing)

1. Never let the LLM state a routing/reachability fact not returned by a tool.
2. FINDINGS (engine) and VERDICT (judgment) stay visually separate in output.
3. Every verdict ends with a residual-unknowns section — config-only analysis
   cannot see live utilization, real session state, timing, or hardware faults.
4. Scope the LLM's toolset to the ~12 relevant tools; hide AWS/compliance/
   discovery tools so the model isn't distracted.
5. Confirm the two schema unknowns (see Misc/docs/03) against source before finalizing
   the translator prompt — do not assume parameter shapes.

## Current status / open items

BUILD COMPLETE through Misc/docs/06 Step 8 (2026-07-06). Implemented and verified
against the live stack:

- Steps 0–1: unknowns resolved (below); stack runs healthy; tools/list
  confirmed 60 tools, our 12+ present under the real names.
- Step 2: `app/mcp_client.py` — sync wrapper over the mcp streamable-http SDK;
  smoke-tested (13-config lab parses clean: 13 devices, CISCO_IOS, 0 errors).
- Steps 3–5: `app/orchestrator.py` — deterministic upload turn (parse GATE),
  LLM tool-use translator loop, ledger + re-stage stacking via an app-side
  `stage_change_snapshot` tool, and the two-zone synthesizer (prompts + vendor
  reference + runtime caveats). LLM backend: **Ollama Cloud** via its
  OpenAI-compatible endpoint (`https://ollama.com/v1`), key in `.env`
  (OLLAMA_API_KEY). Models: translator `qwen3-coder:480b`, synthesizer
  `gpt-oss:120b` (the only tool-capable models accessible on this key's tier —
  premium models like glm-5.2/deepseek-v4 require a subscription). Override
  via NETGUARD_TRANSLATOR_MODEL / NETGUARD_SYNTHESIZER_MODEL.
- Step 6: `app/streamlit_app.py` — upload widget, chat loop, two-zone render,
  ledger sidebar, tool-call inspector, reset-with-cleanup.
- Step 7 (deterministic layer): validated on the lab via the app's own tool
  path — Scenario 1 (shut AS1–AS2 link): traffic reroutes AND SELECTS the AS3
  transit path (as1core1→as1border2→as3border2→as3core1→as3border1→as2border2→
  …→as2dept1), all flows survive → GO facts. Stacked scenario (also shut
  AS1–AS3): AS1 fully isolated from 2.128.0.0/16 → NO-GO facts. Re-stage
  stacking (change1→change2) and per-snapshot diff queries verified.
- Step 7 (LLM layer, Ollama Cloud): Scenario 1 validated end-to-end. Final run:
  translator probed from the interior device on base AND change1, the
  fragment-edit guard rejected a partial config (the model recovered via
  read_config and resubmitted the full file), and the synthesizer returned
  VERDICT: GO, Confidence High, with the correct AS3 reroute path, tagged
  reasoning, rollback, and residual unknowns — matching the hand analysis in
  Misc/reference/sample_scenarios.md. Earlier runs produced a vantage-point-biased
  NO-GO, fixed by the structural safeguards below.
- Step 8: tool scoping via `x-mcp-tools` header verified (41 vs 60 tools; aws/
  compliance/github hidden); MCP image pinned by digest; snapshot cleanup on
  session reset implemented.

UPGRADE (2026-07-08, from studying github.com/batfish/pybatfish): the app now
also talks to the engine DIRECTLY via pybatfish (`app/engine_direct.py`,
localhost:9996) alongside the MCP tools, unlocking questions the MCP layer
hides. Validated on the lab through the app's own tool path:

- STACKING REWRITTEN (Misc/docs/04 Case A is real): `fork_snapshot` accepts LISTS of
  nodes/interfaces to deactivate. New translator tool `apply_failure_set`
  accumulates the failure set in the ledger and applies it as ONE engine fork —
  no config editing, no re-parse. "Now also fail X" = one more call. Config
  EDITS still re-stage (stage_change_snapshot); edits and failures COMPOSE
  (a new edit snapshot automatically gets the active failure set re-forked on
  top, e.g. base -> change1 -> fail3). Verified: single failure reroutes via
  AS3; stacked failure isolates AS1; compose keeps failures across edits.
- SESSION STATUS IS NOW AN ENGINE FACT: `bgp_session_status` (bgpSessionStatus)
  returns per-neighbor ESTABLISHED/NOT_ESTABLISHED/NOT_COMPATIBLE. On the lab
  base it finds the 3 seeded dead peers on as1border1. The synthesizer caveat
  changed accordingly (only LIVE state remains a residual unknown).
- NATIVE DIFF: `differential_reachability` (differentialReachability) returns
  exactly the flows whose disposition changed between two snapshots (compact
  before/after dispositions). This replaces hand-compared traceroutes as the
  authoritative "what did THIS change break".
- NEW CHECKS: `detect_loops` (forwarding loops — now engine-backed before any
  GO), `health_checks` (initIssues + undefinedReferences + duplicate interface
  IPs + BGP session summary + loops; finds the seeded duplicate loopback
  2.1.1.2/32), `routes_to` (main-RIB selected routes for a prefix — best-path
  facts now available).

DEMO SCENARIOS (2026-07-08): `scenarios/` holds five self-contained demos
built from the official pybatfish sample networks + notebooks (failure/chaos
monkey, AS-lab failover, ACL/firewall, BGP session debugging, route
analysis). Each folder = configs/ to upload + QUESTIONS.txt (questions with
notebook-derived expected outcomes) + topology PNG where upstream has one.
All five sets verified to parse cleanly through the app's upload turn.

UI (2026-07-08): `app/ui_helpers.py` (pure, unit-tested) + streamlit rewrite —
human-readable ledger sidebar (snapshot chain, active failures, change
history), full-width markdown FINDINGS/VERDICT (no scroll frames), a parsed
"Verdict at a glance" table (dash-normalized field parsing — models emit
U+2011 in PACKET-FLOW etc.), a deterministic "Engine facts" table built from
the tool log (traceroutes, BGP summary, diffs, failure set, loops, health
counts), and Graphviz topology diagrams from `layer3_edges` (base topology on
snapshot build; BEFORE/AFTER side-by-side per change turn — lost links dashed
red, failed nodes red).

Remaining / caveats:
- Two upstream bugs found and worked around:
  1. `network_traceroute`/`network_bidirectional_reachability` ignore
     BATFISH_HOST (default host="localhost" inside the container) — the client
     passes `host` explicitly (BATFISH_ENGINE_HOST env, default "batfish").
  2. `batfish_failure_impact` built an invalid fork-snapshot name (enum repr
     with '.', plus '/' from interface names) — patched copy mounted over the
     container file via docker/patches/ (see docker-compose.yml volumes).
     Consider upstreaming both fixes.
- `failure_impact` reports IMPACT only for flows that STOP working; a survived
  reroute shows NO_IMPACT. Path-change facts come from traceroute diffs — the
  translator tool descriptions encode this.
- LLM-layer lessons baked into the orchestrator (found during end-to-end
  validation on Ollama Cloud):
  1. Vantage points — a translator probing reachability only from the device
     whose interface it just shut gets NO_ROUTE and produces a wrong NO-GO
     even when interior traffic reroutes fine. The runtime addendum now
     requires probing from at least one interior device and treating
     failure_impact/traceroute disagreement as a vantage-point conflict.
  2. Fragment edits — models sometimes pass only the changed stanza to
     stage_change_snapshot instead of the full file, silently gutting the
     device config. The app now rejects any edited file that shrinks below
     50% of its previous size and tells the model to retry with the full text.

All four Step-0 unknowns RESOLVED (2026-07-06) from upstream source
(`Presidio-Federal/batfish-mcp-container`, cloned and read):

- [x] `batfish_failure_impact` input schema: `FailureImpactInput` takes ONE
      `failure_type` (node|interface) and ONE `target: str` — NO multi-failure
      list. Stacked failures therefore use the re-stage approach (Misc/docs/04
      Case B), each downed interface expressed as a `shutdown` config edit.
      (Internally the tool uses pybatfish `bf.fork_snapshot(deactivate_*)` and
      diffs a full traceroute matrix — but forking is not exposed as a tool.)
- [x] `batfish_check_routing` output: PASS/FAIL + protocol **process
      configuration presence** only (bgpProcessConfiguration / nodeProperties).
      It does NOT return session status or best-path/RIB — the docs' original
      assumption was wrong. No registered tool returns BGP session status or
      RIB. Verdict facts must come from traceroute / simulate_traffic /
      failure_impact / bidirectional_reachability / analyze_acl_rules; session
      state is a RESIDUAL-UNKNOWN.
- [x] Transport: FastMCP **streamable-http** (TRANSPORT env, default
      streamable-http), port **3009** (not 8080), path `/mcp/`,
      `DISABLE_JWT_AUTH=true` required for local use. Env vars: BATFISH_HOST,
      BATFISH_PORT, TRANSPORT, PORT, HOST. docker-compose.yml updated to match.
- [x] No `fork_snapshot` tool — confirmed in upstream server.py registrations.
      Also: `network_compare_snapshots` has a tool file upstream but is NOT
      registered — snapshot diffs are computed app-side by running the same
      query bundle against both snapshots.

Additional Step-0 facts the implementation relies on:
- Real registered tool names are prefixed: `initialize_network_prepare_snapshot`,
  `initialize_network_upload_zip`, `initialize_network_finalize_snapshot`,
  `initialize_snapshot`, `management_get_parse_status`,
  `management_get_snapshot_info`, `management_list_snapshots`,
  `management_delete_snapshot`, `batfish_check_routing`,
  `batfish_simulate_traffic`, `batfish_failure_impact`, `network_traceroute`,
  `network_bidirectional_reachability`, `network_analyze_acl_rules`,
  `batfish_run_tagged_tests`.
- `initialize_snapshot(network, snapshot, configs: Dict[filename → content])`
  initializes directly from a dict — no zip/staging needed. This is the app's
  upload path AND the re-stage path for edits.
- Tool scoping (Step 8) is an `x-mcp-tools` HTTP header (toolset names or
  individual tool names), enforced by upstream middleware.
