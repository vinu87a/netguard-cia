<p align="center">
  <img src="logo.png" alt="NetGuard-CIA" width="260"/>
</p>

# NetGuard-CIA — Network Change-Impact Analysis

**Ask your network "what if?" in plain English. Get a Go / No-Go verdict backed
by deterministic analysis — not LLM guesswork.**

NetGuard-CIA is a chat application for network engineers. Upload your device
configurations (Cisco IOS / Juniper Junos), then ask questions like:

> *"What breaks if I shut the link between as1border1 and as2border1?"*
> *"Now also fail the backup link — can AS1 still reach 2.128.0.0/16?"*
> *"Are there any problems with the current configs?"*

Every answer is a structured **verdict** (GO / GO-WITH-CONDITIONS / NO-GO /
INSUFFICIENT-DATA) with the verified facts, the checks that were run, a
rollback plan, and before/after topology diagrams.

## The one principle

**[Batfish](https://www.batfish.org/) computes every network fact. The LLM only
translates your intent into analysis queries and writes up the verdict.**
If the answer asserts a routing or reachability fact that didn't come from the
analysis engine, that's a bug. Every reasoning step in a verdict is tagged with
its provenance: `[verified]` (computed by the engine), `[vendor-doc]` (a
documented Cisco/Junos behavior), or `[judgment]` (the model weighing
significance) — and confidence is capped by the weakest link in that chain.

## What it looks like

- **Verdict banner** — color-coded GO ✅ / conditions ⚠️ / NO-GO ⛔ headline
- **Verdict at a glance** — confidence, impacted services, packet flow, rollback
- **Network checks performed** — every analysis run, its target, and its result
- **Findings & full verdict** — the evidence base and the tagged reasoning
- **Topology diagrams** — your network on upload; before/after side-by-side
  when a change is simulated (lost links dashed red, failed devices red)
- **Session state** — a running timeline of simulated failures and config
  changes; scenarios stack ("now also fail X" builds on the previous step)

## Architecture

```
┌───────────────────────────────────────────┐
│  Streamlit chat UI  (app/)                │  upload configs, ask scenarios
└───────────────┬───────────────────────────┘
                │  LLM translator + synthesizer (Ollama Cloud,
                │  OpenAI-compatible API — prompts in prompts/)
┌───────────────▼───────────────────────────┐
│  Batfish MCP server (docker, port 3009)   │  snapshot mgmt + analysis tools
│  + direct pybatfish access (port 9996)    │  sessions, diffs, forks, loops
└───────────────┬───────────────────────────┘
┌───────────────▼───────────────────────────┐
│  Batfish engine (docker)                  │  the deterministic analysis core
└───────────────────────────────────────────┘
```

The LLM runs a tool-use loop against a curated set of analysis actions:

| Check | What it computes |
|---|---|
| Failure simulation | fails nodes/interfaces as one engine fork; stacks across turns |
| Path trace | hop-by-hop forwarding with ACL decisions and disposition |
| Traffic simulation | can a described flow get through, and how is it disposed |
| Two-way reachability | catches asymmetric routing and one-way blocks |
| BGP session check | per-neighbor session establishment (as modeled) |
| Before/after comparison | exactly which flows changed disposition between states |
| Loop check | forwarding loops in a changed network |
| Route lookup | which routes were actually selected into the RIB |
| Configuration health | parse issues, undefined refs, duplicate IPs, dead peers |
| Configuration change | applies a config edit, re-validates, advances the timeline |

Config **edits** rebuild the network model; **failures** are cheap engine forks
of the current state — and the two compose, so an edit made while failures are
active keeps those failures applied.

## Quickstart

Prereqs: Docker (≈4 GB RAM free for the engine), Python 3.11+, an
[Ollama Cloud](https://ollama.com) API key.

```bash
git clone https://github.com/vinu87a/netguard-cia.git
cd netguard-cia

# 1) analysis stack (two containers, images pinned)
cd docker && docker compose up -d --wait && cd ..

# 2) python env
python3 -m venv .venv && .venv/bin/pip install -r app/requirements.txt

# 3) LLM credentials — create .env at the repo root:
echo "OLLAMA_API_KEY=your-key-here" > .env

# 4) run
.venv/bin/streamlit run app/streamlit_app.py
```

Open http://localhost:8501, upload configs from one of the demo scenarios,
click **Build snapshot**, and ask away.

### Demo scenarios

`scenarios/` contains five self-contained demos built from the official
[Batfish examples](https://github.com/batfish/pybatfish) — each folder has the
configs to upload, a `QUESTIONS.txt` with questions and expected outcomes, and
a topology diagram:

| Scenario | Network | Theme |
|---|---|---|
| 1 — failure impact & chaos monkey | 13 city routers | node/link failures, stacked "now also fail X" |
| 2 — link failure with failover | classic 3-AS lab | failover verdicts, stacking, seeded defects |
| 3 — ACL & firewall rules | 2 devices | filter permit/deny with crisp expected answers |
| 4 — BGP session debugging | 3-AS lab variant | every broken-session flavor, on purpose |
| 5 — route analysis | 2 routers | small enough to verify every fact by hand |

### Configuration

| Setting | Default | Purpose |
|---|---|---|
| `OLLAMA_API_KEY` (.env) | — | LLM credentials (required) |
| `OLLAMA_BASE_URL` | `https://ollama.com/v1` | any OpenAI-compatible endpoint works |
| `NETGUARD_TRANSLATOR_MODEL` | `qwen3-coder:480b` | the tool-calling model |
| `NETGUARD_SYNTHESIZER_MODEL` | `gpt-oss:120b` | the verdict-writing model |
| `BATFISH_MCP_URL` | `http://localhost:3009/mcp/` | analysis server endpoint |
| `BATFISH_DIRECT_HOST` | `localhost` | direct engine host |

## Project structure

```
app/                    the Streamlit app (runtime)
  streamlit_app.py        chat UI: upload, verdict tables, topology
  orchestrator.py         translator loop, session ledger, synthesizer
  mcp_client.py           analysis-server client
  engine_direct.py        direct engine questions (forks, sessions, diffs)
  ui_helpers.py           verdict parsing / checks table / topology DOT
prompts/                LLM system prompts (loaded at runtime)
docker/                 the analysis stack (compose, pinned images, patches)
scenarios/              five ready-made demos (configs + questions)
Misc/                   design docs and reference material (not needed to run)
CLAUDE.md               project brief + build log (for AI-assisted development)
```

## Design guarantees

- **Parse gate** — no verdict is produced on a half-parsed config set.
- **Two-zone answers** — verified findings are always visually separate from
  judgment, and every reasoning step carries a provenance tag.
- **Mandatory residual-unknowns** — config-only analysis cannot see live
  utilization, real-time session state, convergence timing, or hardware faults;
  every verdict says so.
- **Guarded edits** — a config edit that fails to parse, or that silently
  gutted a device file, is rejected and the session state does not advance.
- **Multi-vantage probing** — reachability conclusions require agreement from
  multiple probe points; a single-source break can only lower the verdict to
  "insufficient data", never to a confident NO-GO.

## Acknowledgments

- [Batfish](https://www.batfish.org/) — the open-source network analysis
  engine that computes every fact in this app.
- [pybatfish](https://github.com/batfish/pybatfish) (Apache-2.0) — the Python
  client used for direct engine questions; the demo networks in `scenarios/`
  come from its example notebooks.
- [Presidio-Federal/batfish-mcp-container](https://github.com/Presidio-Federal/batfish-mcp-container)
  (MIT) — the MCP wrapper around Batfish. `docker/patches/` carries a small
  fix (derived from that MIT-licensed code) for a snapshot-naming bug in its
  failure-impact tool, which the compose file mounts over the container.

## Disclaimer

NetGuard-CIA analyzes **configurations**, not live networks. Verdicts model
what the configs imply; they cannot see the state of running devices. Always
pair a GO with your own change-management process.
