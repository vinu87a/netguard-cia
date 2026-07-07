# 05 — Output Contract (two-zone answer)

Every scenario answer has two visibly separate zones. Blending them lets a wrong
judgment inherit the authority of an engine fact — forbidden.

## Zone 1 — FINDINGS (engine facts only)
Only what Batfish tools returned. Each line traceable to a specific tool call. No
interpretation, no verdict language.

```
FINDINGS (from Batfish)
- BGP session as1border1↔as2border1: <status from check_routing_tool>
- Selected path for 2.128.0.0/16 from AS1: <from check_routing_tool>
- Reachability AS1→2.128.0.0/16 after change: <from simulate_traffic_tool>
- Traceroute AS1→dept: <from network_traceroute_tool>
- Diff vs previous snapshot: <what changed>
```

Everything here is `[engine]` by construction.

## Zone 2 — VERDICT (judgment, tagged)
The Go/No-Go and reasoning. Every inferential step carries a tag:
- `[engine]` — restating a Batfish fact.
- `[vendor-doc]` — applying a Cisco/Junos rule from vendor_reference.md.
- `[judgment]` — the LLM weighing significance/severity.

```
VERDICT: <GO | GO-WITH-CONDITIONS | NO-GO | INSUFFICIENT-DATA> — <one line>

CONFIDENCE: <High | Medium | Low>
  drivers: <engine-backed vs inferred; config completeness>

IMPACTED SERVICES / COMPONENTS:
  <named — e.g. "AS2 departmental service (2.128.0.0/16)">

PACKET-FLOW:
  <per named flow: survives / reroutes / breaks + where>

REASONING:
  - <step> [engine]
  - <step> [vendor-doc]
  - <step> [judgment]

CONDITIONS (if GO-WITH-CONDITIONS):
  - <precondition / sequencing / mitigation>

ROLLBACK:
  <steps to revert; is revert clean? yes/no>

RESIDUAL-UNKNOWNS (mandatory):
  Config-only analysis cannot see: live utilization, real-time session state,
  timing/convergence, hardware/optical faults, control-plane scale limits.
```

## Verdict decision logic (first match sets the floor)
1. **INSUFFICIENT-DATA** — material device/peer absent from the model, OR a
   verdict-critical fact had no engine backing.
2. **NO-GO** — breaks a named service/flow with no engine-verified failover, OR
   creates a loop/blackhole/critical-session teardown, OR large + hard to revert.
3. **GO-WITH-CONDITIONS** — safe only with listed preconditions/sequencing/
   mitigations.
4. **GO** — no named-service loss, failovers engine-verified, no loops,
   reversible.

## Confidence capping
Confidence is capped by the weakest provenance in the chain. If any
verdict-critical step is `[judgment]` or `[inferred]` rather than `[engine]`,
confidence cannot be High.
