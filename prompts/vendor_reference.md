# Vendor Reference — Cisco IOS & Juniper Junos

Grounding for vendor-specific behavior. Facts derived here are tagged
`[vendor-doc]`. Sources: Cisco BGP Best-Path (Doc ID 13753); Junos OS BGP User
Guide.

Note: Batfish models both vendors' best-path selection internally, so for a
single-snapshot route/best-path question you rely on the tool result, not this
file. This file matters for MIGRATION reasoning and for explaining WHY a result
differs across vendors.

## BGP best-path order (differs)
- Cisco: WEIGHT → LOCAL_PREF → locally-originated → AS_PATH → origin → MED →
  eBGP-over-iBGP → IGP metric → oldest → router-id.
- Junos: route-preference (BGP default 170) → LOCAL_PREF → AS_PATH → origin →
  MED → eBGP-over-iBGP → IGP cost → router-id/oldest.
- WEIGHT is Cisco-only. Junos's preference step is FIRST and has no Cisco analog
  there. A migration that relied on weight must re-express intent (usually via
  local-pref/policy) — equivalence is a claim to prove.

## Routing-policy terminal action (the top migration trap)
- Cisco route-map: first-match; unmatched routes are IMPLICITLY DENIED.
- Junos policy-statement: term-ordered; on no-match, falls through to the default
  policy, which for BGP is DEFAULT-ACCEPT.
- A Cisco route-map ported to Junos WITHOUT a terminal `then reject` will silently
  accept/advertise routes Cisco would have dropped. Flag as high-severity in any
  IOS→Junos migration verdict.

## Admin distance / route preference
- Cisco: eBGP 20, iBGP 200. Junos: BGP 170 (flat). Affects redistribution
  outcomes when comparing across vendors.

## Filter drop semantics
- Both: ordered first-match with implicit drop.
- Cisco `deny` ≈ Junos `discard` (silent) vs `reject` (returns ICMP). Preserve
  which — it changes what a troubleshooter observes.
