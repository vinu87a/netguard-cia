# Sample Scenarios (13-config AS1/AS2/AS3 lab)

Use these to validate the pipeline. Expected verdicts are from hand analysis —
confirm the tool-backed run agrees.

## The lab
Three ASes (AS1, AS2, AS3) each with iBGP + route reflectors and eBGP
interconnects, plus an AS2 department in AS65001 (prefixes 2.128.0.0/24,
2.128.1.0/24, aggregated to 2.128.0.0/16). Known defects seeded in the configs:
dead eBGP peers on as1border1 (AS666, AS555, stray 3.2.2.2), duplicate loopback
2.1.1.2/32 on as2border2 and as2dept1, MTU mismatches on as2core2 (1800/1600/1700).

## Scenario 1 — link failure with working failover
"What breaks if I shut the AS1–AS2 direct link (as1border1↔as2border1)?"
- Expected: reachability AS1↔AS2 SURVIVES via AS3 transit (AS1→AS3→AS2), because
  AS3 re-advertises AS2's aggregate (ACL 102 catches it in as3_to_as1). AS-path
  lengthens 2 → 3 2.
- Expected verdict: GO or GO-WITH-CONDITIONS (confirm AS3 transit path is
  SELECTED, not merely present — this is the hinge).

## Scenario 2 — change that isolates a service
"What if I add an inbound filter on as3border1 that drops AS2's prefixes?"
- Expected: with the AS1–AS2 direct link also down, AS1 loses the AS2 dept
  prefix entirely (no transit path left).
- Expected verdict: NO-GO (named service unreachable, no verified failover).

## Scenario 3 — selective local-pref (the silent hairpin)
"What if I lower local-preference from 350 to 150 only on the AS2-inbound
route-map at as1border1?"
- Expected: AS1 prefers the longer AS3-transit path over the healthy direct link
  (local-pref beats AS-path in selection). Traffic hairpins through AS3 while the
  direct link stays up.
- Expected verdict: GO-WITH-CONDITIONS or NO-GO depending on whether the hairpin
  is acceptable — the point is the tool should SHOW the egress flip.

## Scenario 4 — baseline health (no change)
"Are there any problems with the current configs?"
- Expected findings from parse-check + routing: dead peers on as1border1,
  duplicate loopback 2.1.1.2, MTU mismatch on as2core2.
- Expected verdict: not a change verdict — a health report. Confirm the
  parse-check surfaces the duplicate loopback and dead sessions.

## Scenario 5 — stacking
"Shut AS1–AS3 link." then "now also shut AS1–AS2 link."
- Expected: after both, AS1 is isolated from AS2 and AS3 directly; check whether
  any path remains. Validates that the second scenario builds on the first
  (ledger/stacking), not on base.
