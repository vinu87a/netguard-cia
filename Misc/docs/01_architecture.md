# 01 — Architecture

## Components

### Batfish all-in-one engine (container 1)
The deterministic core. Parses device configs into a vendor-neutral model and
answers "questions" about network behavior — BGP session status, RIB, best-path
selection, reachability, differential reachability, filter/ACL behavior. Requires
only configs; does NOT touch live devices. Holds snapshots in memory.

- Image: `batfish/allinone` (or `batfish/batfish`)
- Ports: 9996, 9997 (service), 8888 (Jupyter, optional — not needed here)
- Stateful: snapshots live for the container's lifetime.

### Batfish MCP container (container 2)
A Model Context Protocol server that wraps the Batfish engine and exposes ~50
tools over MCP. This is the layer the LLM talks to. Built from
`Presidio-Federal/batfish-mcp-container` (MIT). Has a middleware layer that can
filter/hide tools — we use it to expose only our ~12.

- Image: `ghcr.io/presidio-federal/batfish-mcp-container` (pin a version tag)
- Talks to the engine via Batfish API; talks to the LLM via MCP.
- Transport: HTTP endpoint (confirm exact path for your client).

### LLM (translator + synthesizer)
Two roles, one model. Front half translates the user's English into Batfish tool
calls and frames snapshots. Back half turns structured Batfish results into a
Go/No-Go verdict. The LLM does NO independent network reasoning — Batfish owns all
facts. Prompts live in `prompts/`.

### Streamlit chat UI
Thin front end. Handles config upload, the chat loop, renders the two-zone
answer. Skeleton in `app/`.

## Data flow

```
user uploads configs
   → UI sends to LLM turn
   → LLM: stage snapshot (prepare → upload → finalize)
   → LLM: get_parse_status  → if failures, surface + stop
   → LLM: get_snapshot_info → confirm model built
   → "ready, ask me a scenario"

user asks "what if <X>"
   → LLM: classify scenario (failure vs edit vs acl vs reachability)
   → LLM: pick tool(s), frame query, (build change snapshot if needed)
   → MCP → Batfish → structured result back
   → LLM: emit FINDINGS (facts) + VERDICT (judgment, tagged)
```

## Why this shape

The design exists to eliminate one failure mode: an LLM confidently tracing a
route-map or reachability path wrong and issuing a Go/No-Go on it. By routing
every fact through Batfish and confining the LLM to translation + synthesis, the
verdict rests on deterministic computation. The provenance tags in the output
make the fact/judgment boundary auditable — the user always sees where Batfish's
certainty ends and the model's opinion begins.

## Vendor scope

Cisco IOS and Juniper Junos for now. Batfish models both natively, including
their different BGP best-path orders and policy semantics. Vendor-specific
migration reasoning (e.g. Junos default-accept vs Cisco implicit-deny on
policies) is grounded in `prompts/vendor_reference.md`, applied by the
synthesizer, and tagged `[vendor-doc]`.
