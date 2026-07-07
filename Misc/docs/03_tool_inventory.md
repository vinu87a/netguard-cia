# 03 — Batfish MCP Tool Inventory

Source of truth: `reference/tools_init.py` (the upstream package `__init__.py`).
The container exposes ~50 tools. We use ~12 and hide the rest via the middleware
tool-filtering layer so the LLM isn't distracted by irrelevant tools.

> Get the DEFINITIVE names + schemas from the running container via the MCP
> `tools/list` method — the middleware may rename/hide relative to source.

## USE — core what-if toolset (~12)

### Snapshot lifecycle & staging
- `network_prepare_snapshot` — begin staging a snapshot.
- `network_upload_zip` — upload configs (zip) into staging.
- `network_view_staging` — inspect what's staged.
- `network_remove_config` — remove a staged config (used when re-staging edits).
- `network_finalize_snapshot` — build the snapshot from staging.
- `init_snapshot_tool` — direct network snapshot init (alternative to staging).

### Snapshot management / ledger
- `list_snapshots_tool` — enumerate snapshots (backs the ledger).
- `get_snapshot_info_tool` — device count / model summary.
- `get_parse_status_tool` — **parse-check gate on upload.**
- `delete_snapshot_tool` — cleanup old change snapshots.

### Analysis (the query bundle)
- `check_routing_tool` (ProtocolType) — RIB / best-path / session status. CONFIRM
  whether it returns best-path AND session status together, or if a separate
  session check is needed.
- `simulate_traffic_tool` — reachability for a described flow.
- `failure_impact_tool` (FailureType) — **purpose-built what-if for failures.**
  CONFIRM whether it accepts MULTIPLE simultaneous failures (decides stacking
  cost for failure scenarios).
- `network_traceroute_tool` — path trace.
- `network_bidirectional_reachability_tool` — both-direction reachability.
- `network_analyze_acl_rules_tool` — ACL/filter behavior for filter scenarios.
- `run_tagged_tests_tool` — run an assertion suite (optional, for health pass).

## MAYBE — situational
- `network_generate_topology_tool` — for the diagram, if UI shows topology.
- `network_summary_tool`, `network_reachability_summary_tool` — overview panels.
- `get_inventory_tool` — device/resource listing.

## HIDE — out of scope for what-if (filter these out)
- All `aws_*` (9): reachability, trace_route, internet_exposure,
  security_evaluation, subnet_segmentation, route_analysis, node_inventory,
  change_impact, find_unrestricted_ssh — cloud/AWS analysis.
- All `initialize_aws_*` (5): AWS snapshot staging.
- All `compliance_*` (6): zone classification, model compliance (ISA-95/Purdue/
  NIST) — not needed for routing what-ifs.
- `github_snapshot_tool` — GitHub-sourced configs (we upload directly).
- Granular discovery tools: `network_vlan_discovery`, `network_vlan_device_count`,
  `network_interface_vlan_count`, `network_device_connections`,
  `network_topology_connections`, `network_node_inventory`,
  `network_classify_devices`, `network_segment`, `network_get_allowed_services`,
  `network_list_boundary_devices`, `network_list_subnets` — useful for discovery,
  not for verdicts. Keep hidden unless a scenario needs one.

## SCHEMA UNKNOWNS — RESOLVED (2026-07-06, from upstream source)

1. `batfish_failure_impact` — **single failure only.** `FailureImpactInput` =
   `{network, snapshot, failure_type: "node"|"interface", target: str}`.
   Interface targets use the format `node[interface]`. Internally it
   `bf.fork_snapshot(deactivate_*)`s and diffs a full no-constraint traceroute
   matrix, returning `{overall: IMPACT|NO_IMPACT, results: [impacted flows]}`.
   → Stacked failures use the re-stage path (docs/04 Case B).
2. `batfish_check_routing` — **health check only.** Input
   `{network, snapshot, protocols: ["bgp"|"ospf"]}`. BGP branch returns
   PASS/FAIL + bgpProcessConfiguration (process presence), NOT session status
   and NOT best-path/RIB. No registered tool returns session status or RIB.
   → Verdict facts come from traceroute / simulate_traffic / failure_impact /
   bidirectional_reachability / analyze_acl_rules. Session state is always a
   RESIDUAL-UNKNOWN in verdicts.

## CONFIRMED REGISTERED NAMES (server.py `@tool(name=...)`)

The middleware registers different names than the source module names.
The ones we use:

| docs name (old)                | REAL registered name                    |
|--------------------------------|-----------------------------------------|
| network_prepare_snapshot       | initialize_network_prepare_snapshot     |
| network_upload_zip             | initialize_network_upload_zip           |
| network_view_staging           | initialize_network_view_staging         |
| network_remove_config          | initialize_network_remove_config        |
| network_finalize_snapshot      | initialize_network_finalize_snapshot    |
| init_snapshot_tool             | initialize_snapshot                     |
| list_snapshots_tool            | management_list_snapshots               |
| get_snapshot_info_tool         | management_get_snapshot_info            |
| get_parse_status_tool          | management_get_parse_status             |
| delete_snapshot_tool           | management_delete_snapshot              |
| check_routing_tool             | batfish_check_routing                   |
| simulate_traffic_tool          | batfish_simulate_traffic                |
| failure_impact_tool            | batfish_failure_impact                  |
| network_traceroute_tool        | network_traceroute                      |
| network_bidirectional_reachability_tool | network_bidirectional_reachability |
| network_analyze_acl_rules_tool | network_analyze_acl_rules               |
| run_tagged_tests_tool          | batfish_run_tagged_tests                |

Key signatures:
- `initialize_snapshot(network, snapshot, configs: Dict[filename→content])` —
  direct init from a dict, no zip/staging. Preferred upload AND re-stage path.
- `network_traceroute(network, snapshot, source_location, dest_ip,
  dest_port=None, ip_protocol="tcp", src_ip=None)`
- `network_bidirectional_reachability(network, snapshot, location_a,
  location_b, ip_a, ip_b, port=None, protocol="tcp")`
- `batfish_simulate_traffic(network, snapshot, src, dst, applications=None)`

Other notes:
- `network_compare_snapshots` exists as a tool FILE upstream but is NOT
  registered — no snapshot-diff tool. Diff app-side: same query bundle against
  both snapshots, compare results.
- Tool scoping: send the `x-mcp-tools` HTTP header with toolset names
  (`initialization,management,batfish,network`) or individual tool names.
- Transport: streamable-http on port 3009, endpoint path `/mcp/`,
  `DISABLE_JWT_AUTH=true` locally.
