# NetGuard-CIA — project brief (for AI-assisted development)

> Read this first. It orients you (an AI agent) to what this project is, how the
> pieces fit, and where things live. Refreshed 2026-07 to match the current
> build (Commotion/Ollama providers, two-mode output, 37 tools, Phase 1–5).

## What we're building

A Streamlit chat app for network engineers. Upload device configs (Cisco IOS /
Juniper Junos), then ask two kinds of question:

- **Change scenarios** ("what breaks if I shut this link?", "is this ACL change
  safe?") → a **Go/No-Go VERDICT**.
- **Read-only questions** ("can X reach Y through acl_in?", "who owns 2.1.3.2?",
  "why is this BGP session down?", "list the devices") → a **direct ANSWER**.

## The one hard rule (shapes every decision)

**Batfish computes every network fact. The LLM only (a) translates intent into
analysis tool calls and (b) writes up the result. The LLM never reasons about
routing/reachability itself.** Any network fact in an answer that did not come
from a tool result is a bug.

## Two output modes (deterministic)

`_scenario_mode(ledger, tool_log)` in `orchestrator.py` classifies each turn:
**CHANGE** iff *this turn* applied a failure/edit (`apply_failure_set` /
`stage_change_snapshot`), else **QUERY**. Keyed on the turn's tool_log, NOT on
the persistent ledger — a read-only follow-up after a change is still a QUERY.
CHANGE → VERDICT (GO / GO-WITH-CONDITIONS / NO-GO / INSUFFICIENT-DATA); QUERY →
ANSWER + STATUS(OK/ATTENTION) + EVIDENCE. Both keep a FINDINGS zone and a
mandatory RESIDUAL-UNKNOWNS line.

## Architecture (3 layers + a provider abstraction)

```
Streamlit UI (app/streamlit_app.py)
   │  LLM: translator + verifier + synthesizer  (app/llm_provider.py)
   ▼
Batfish MCP server (docker :3009)   +   direct pybatfish (:9996, app/engine_direct.py)
   ▼
Batfish engine (docker) — the deterministic analysis core
```

The translator drives a tool-use loop over **37 tools** (`TRANSLATOR_TOOLS` in
`orchestrator.py`). Most tools go **direct to the engine via pybatfish**
(`engine_direct.py`) — that's where the depth is; a handful (traceroute, ACL
analysis, snapshot lifecycle) go through the MCP server (`mcp_client.py`).

## LLM backend (`app/llm_provider.py`, select via `NETGUARD_LLM_PROVIDER`)

- **Commotion** (Tata "AI worker") — the current default. ~17s/round-trip (was
  ~79s during an outage). NO native function-calling → the app emulates it (the
  worker emits a `{"tool":..,"args":..}` JSON object). Behavioral personas live
  **server-side** on the Commotion platform, so the app sends **thin** messages
  (`ROLE: <TRANSLATOR|VERIFIER|SYNTHESIZER>` + data + a per-message format hint).
- **Ollama** — the fallback, and **faster** (~1–2s/call), with **native**
  tool-calls. Cloud (`qwen3-coder:480b` translator, `gpt-oss:120b` synth) or
  local (`OLLAMA_BASE_URL=http://localhost:11434/v1`). Ollama has no server-side
  persona, so `OpenAIProvider` **prepends the full role prompt** from
  `prompts/ollama_{translator,verifier,synthesizer}.md`.
- Switch backends: set `NETGUARD_LLM_PROVIDER` in `.env` and (for Commotion) keep
  the branch in `build_provider()` enabled. Both are fully wired.
- See memory `llm-backend-state` for current status.

### Prompts — two parallel sets, keep them in sync
- **Commotion**: `Misc/docs/08_commotion_worker_persona.md` holds §1 worker
  router, §2 translator, §3 verifier, §4 synthesizer. **The user pastes these
  into the Commotion platform** — editing the doc does NOT auto-update the live
  worker.
- **Ollama**: `prompts/ollama_*.md` (app-loaded). These duplicate the same
  recipes / completeness-bar / two-mode content; update both together.

## The turn flow (`run_scenario_turn`)

1. **Upload turn** (`run_upload_turn`) — init snapshot, **parse GATE** (no verdict
   on a half-parsed set), device inventory.
2. **Translator loop** (`_translator_rounds`) — picks tools per the SCENARIO
   RECIPES; each call is **pre-flight validated** then executed; a
   CHECKS-ALREADY-RUN recap rides each result.
3. **Deterministic gate floor** (change turns) — `snapshot_gates` runs 6
   pybatfish assertions; only gates that **regress vs base** force NO-GO.
4. **Deterministic completeness/soundness review** (`_deterministic_verify`,
   change turns only) — a code checklist over the tool_log (specific-flow probe,
   before/after, loop check, multi-vantage-on-a-negative, blast-radius conflict).
   If something's missing → ONE remediation nudge → re-check. **No verifier LLM
   call** (it was replaced 2026-07 — the old adversarial LLM verifier caused
   churn and cost a round-trip per turn).
5. **Synthesizer** — two-zone output, mode-appropriate (VERDICT or ANSWER).

## Robustness layer (why the engine never crashes on bad LLM args)

- **Arg coercions** (`orchestrator.py`): `_coerce_failure_args`,
  `_normalize_node_spec` (tolerates lists / raw regex), `_coerce_reachability_args`
  (fixes actions/headers shape + IP-as-location).
- **Generic pre-flight validator** `_validate_tool_args` — checks each call
  against its JSON schema (unknown tool / missing required / object-as-string)
  and bounces a fix back to the translator BEFORE the engine sees it.
- A broad `except` around tool execution catches anything else — a bad arg is
  always a recoverable ERROR result, never a dead turn or a wedged engine.

## Tool inventory (37, by domain)

- **Reachability/forwarding**: network_traceroute, batfish_simulate_traffic,
  network_bidirectional_reachability, reachability_search (proof engine),
  differential_reachability, detect_loops, multipath_consistency.
- **Routing tables**: routes_to, bgp_rib, prefix_tracer, differential_query
  (generic base-vs-change diff of any table question).
- **BGP**: bgp_session_status, bgp_compatibility, bgp_edges, bgp_peer_config.
- **OSPF**: ospf_compatibility, ospf_edges, ospf_process_config.
- **Route policy**: test_route_policy, search_route_policy.
- **ACL/filter**: test_filter, search_filter, compare_filters,
  filter_line_reachability, find_matching_filter_lines, network_analyze_acl_rules.
- **Inventory / Q&A**: node_properties, ip_owners.
- **Health/gates**: health_checks, snapshot_gates (auto), batfish_check_routing.
- **Change/state**: apply_failure_set (fork with deactivations),
  stage_change_snapshot (config edit re-stage), read_config, get_snapshot_info.

Build history: Phase 1 (assertion gates + generic differential), Phase 2
(route-policy + provably-safe filters), Phase 3 (BGP/OSPF depth), Phase 4
(counterexample proof + integrity/hygiene), Phase 5 (inventory/Q&A). See memory
`pybatfish-coverage-roadmap`.

## Where to find things

- `app/orchestrator.py` — the core: `TRANSLATOR_TOOLS`, `_execute_translator_tool`
  dispatch, `_translator_rounds`, gates, `_deterministic_verify`, `_scenario_mode`,
  synthesizer.
- `app/engine_direct.py` — direct pybatfish questions (most of the 37 tools).
- `app/mcp_client.py` — MCP client (traceroute, ACL analysis, snapshot mgmt).
- `app/llm_provider.py` — `CommotionProvider`, `OpenAIProvider` (Ollama),
  `FallbackProvider`, `build_provider`.
- `app/ui_helpers.py` — verdict/answer parsing, checks table, topology figures.
- `app/streamlit_app.py` — UI: upload, two-mode render, rich history replay,
  before/after topology.
- `Misc/docs/08_commotion_worker_persona.md` — the 4 Commotion sub-agent prompts.
- `prompts/ollama_*.md` — Ollama role prompts (app-loaded).
- `docker/docker-compose.yml` — the two-container stack (+ `docker/patches/`).
- `scenarios/` — 5 demo config sets + `QUESTIONS.txt`.
- `requirements.txt`, `run.sh` — pinned deps + one-command local start.
- `README.md` — user-facing. `DEPLOY.md` — Rocky Linux production runbook.
- `Misc/docs/` — original design docs (dated; treat as history, verify vs code).

## Running / deploying

- **Local**: `./run.sh` (Docker stack up + waits for health + venv + Streamlit
  on 8501). Needs Docker running and a `.env`.
- **Prod**: `DEPLOY.md` — Rocky Linux (Docker CE, python3.11, SELinux `,Z` on the
  compose bind-mount, systemd, optional nginx+TLS; internal-IP TLS is self-signed,
  not Let's Encrypt).
- **`.env`** (repo root, gitignored — NEVER commit): `NETGUARD_LLM_PROVIDER` +
  `COMMOTION_*` (or `OLLAMA_API_KEY`).
- **After a code change, fully restart** — Streamlit's hot-reload does NOT reload
  imported modules (`orchestrator.py`, etc.).

## Non-negotiables

1. Never let the LLM state a routing/reachability fact not returned by a tool.
2. FINDINGS (engine facts) stay visually separate from VERDICT/ANSWER (judgment);
   every reasoning step is provenance-tagged (`[verified]`/`[vendor-doc]`/`[judgment]`).
3. Every verdict/answer ends with RESIDUAL-UNKNOWNS (config-only analysis can't
   see live utilization, real session state, timing, or hardware faults).
4. Bad tool args never reach the engine (coercions + pre-flight validator).
5. A change scenario gets a verdict; a read-only question gets an answer — never a
   spurious GO for a lookup.

---

## Reference (Batfish/MCP facts — still current)

Resolved from upstream source (`Presidio-Federal/batfish-mcp-container`) +
pybatfish (installed `2025.07.07`):

- **MCP transport**: FastMCP streamable-http, port **3009**, path `/mcp/`,
  `DISABLE_JWT_AUTH=true` locally. Engine on **9996/9997**.
- **Tool scoping**: `x-mcp-tools` HTTP header (upstream middleware) — the app
  hides AWS/compliance/github toolsets (41 of 60 exposed).
- **No `fork_snapshot` MCP tool** — but pybatfish `bf.fork_snapshot(deactivate_
  nodes=[...], deactivate_interfaces=[...])` takes LISTS; `apply_failure_set`
  uses it for one-fork stacked failures. `network_compare_snapshots` is NOT
  registered — diffs are computed app-side (differential questions).
- **`batfish_check_routing`** returns only PASS/FAIL + protocol *process
  presence* (not session status or RIB) — session status comes from
  `bgpSessionStatus`, RIB from `routes`/`bgpRib`.
- **Two upstream bugs worked around**: (1) `network_traceroute` /
  `network_bidirectional_reachability` ignore `BATFISH_HOST` (client passes
  `host` explicitly); (2) `batfish_failure_impact` built an invalid fork name —
  patched copy mounted via `docker/patches/` (needs SELinux `,Z` on Rocky).
- **Registered MCP tool names are prefixed**: `initialize_*`, `management_*`,
  `batfish_*`, `network_*`. `initialize_snapshot(network, snapshot, configs:
  {filename: content})` inits directly from a dict — the app's upload + re-stage path.
