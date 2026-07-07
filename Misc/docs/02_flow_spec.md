# 02 — Flow Spec

Two turn types: **upload turn** and **scenario turn**. The LLM holds a small
snapshot ledger (see docs/04) across turns so scenarios stack.

## Upload turn

Trigger: user provides config files.

1. `network_prepare_snapshot` — start a staging area.
2. Upload configs into staging (`network_upload_zip` for a zip, or the
   add-config path for individual files; `network_view_staging` to confirm).
3. `network_finalize_snapshot` — build the snapshot; name it `base`.
4. `get_parse_status` — **mandatory gate.** If any file failed to parse or has
   conversion warnings that matter, surface them to the user in plain English and
   do NOT proceed to scenarios until acknowledged. A verdict on a half-parsed
   config is worse than no verdict.
5. `get_snapshot_info` — confirm device count / model built as expected.
6. Set ledger: `base` = the finalized snapshot, `current` = `base`.
7. Reply: brief confirmation + "ask me a what-if scenario."

## Scenario turn

Trigger: user asks a plain-English what-if.

1. **Classify** the scenario into one of:
   - *failure* (shut a link, fail a node) → `failure_impact_tool`
   - *policy/config edit* (change local-pref, add/remove neighbor, edit ACL) →
     requires a change snapshot (see docs/04, re-stage approach)
   - *reachability question* (can X reach Y after change) → `simulate_traffic_tool`
     / `network_bidirectional_reachability_tool`
   - *acl question* → `network_analyze_acl_rules_tool`
2. **Frame + compute.**
   - Failure scenario: call `failure_impact_tool` against `current`. (Confirm
     whether it accepts multiple simultaneous failures — decides stacking cost.)
   - Edit scenario: build a new snapshot layered on `current` per docs/04, then
     run the query bundle against it and diff vs `current`.
3. **Query bundle** (run the relevant subset, not just one):
   - `check_routing_tool` (BGP) — sessions + selected best paths.
   - `simulate_traffic_tool` — does the described flow get through.
   - `network_traceroute_tool` / `network_bidirectional_reachability_tool` —
     path survives / where it breaks.
   - `network_analyze_acl_rules_tool` — for filter-scoped scenarios.
   Reconcile them: a session may drop while reachability holds via another path —
   report both, don't stop at the first tool.
4. **Synthesize** (see docs/05): FINDINGS block (engine facts only) then VERDICT
   block (Go/No-Go, tagged judgment, residual-unknowns).
5. **Advance ledger**: if the scenario created a new snapshot, add it to the
   chain and move `current` to it, so "now also …" stacks correctly.

## Default diff semantics for stacking

- "what did THIS change break" → diff new snapshot vs immediately-previous
  (`current` before this turn). This is the default.
- "versus where we started" → diff new snapshot vs `base`. Only when the user
  asks for it explicitly.

## Failure/ambiguity handling

- If the scenario is ambiguous ("change the policy" — which one?), the LLM asks
  ONE clarifying question rather than guessing a delta. A wrong delta produces a
  correct Batfish answer to the wrong question — the worst outcome.
- If a required device/peer for the scenario isn't in the parsed model, return
  INSUFFICIENT-DATA and name what's missing.
