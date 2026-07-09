"""
NetGuard-CIA orchestrator: upload turn, translator loop, snapshot ledger,
synthesizer.

The hard rule (CLAUDE.md): Batfish computes the facts; the LLM only translates
intent into tool calls and synthesizes the verdict. The translator here is a
Claude tool-use loop whose tools map 1:1 onto Batfish MCP calls (plus one
app-side tool, stage_change_snapshot, that implements the re-stage stacking
approach from docs/04). The synthesizer sees ONLY the structured tool results.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from engine_direct import DirectEngine
from llm_provider import build_provider
from mcp_client import BatfishOps, MCPToolError
from ui_helpers import (FRIENDLY_CHECK, split_findings_verdict,
                        strip_internal_terms)

REPO_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_DIR / "prompts"


def _load_dotenv() -> None:
    """Minimal .env loader (repo root) — no extra dependency."""
    env_file = REPO_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

# LLM backend selection lives in llm_provider.py. Default is Commotion only
# (no fallback). The worker owns all behavioral instructions; the app sends
# only ROLE + data + (for the translator) the machine-generated check catalog.

# No context caps — the worker has a large context window, so tool results and
# the synthesizer payload are sent in full. Only a loop backstop remains, to
# prevent a runaway tool-calling loop (not a context limit).
MAX_TRANSLATOR_ITERATIONS = 40
# Verify -> remediate -> re-verify: how many times the verifier may send the
# translator back for missing checks before the verdict proceeds with a floor.
MAX_VERIFY_CYCLES = 5

# Minimal per-message output-format reminders. The domain rules live in the
# worker/sub-agent prompts; these only restate the OUTPUT SHAPE, which this
# platform's models won't reliably honor from the system prompt alone.
_TRANSLATOR_FORMAT = (
    'To run a check, reply with EXACTLY ONE JSON object and nothing else:\n'
    '{"tool": "<check name from AVAILABLE CHECKS>", "args": { ...exactly the '
    'keys in that check\'s schema... }}\n'
    'Use the schema\'s argument keys verbatim (e.g. apply_failure_set takes '
    '"node_failures" and "interface_failures" arrays of strings; interfaces '
    'as "node[Interface]"). One check per reply. When the investigation is '
    'complete, reply in plain text beginning with the line: READY FOR SYNTHESIS'
)
_VERIFIER_FORMAT = (
    'Reply with EXACTLY ONE JSON object and nothing else:\n'
    '{"complete": true|false, "missing_probes": [...], "concerns": [...], '
    '"recommended_floor": "GO"|"GO-WITH-CONDITIONS"|"NO-GO"|'
    '"INSUFFICIENT-DATA"|null}'
)
_SYNTHESIZER_FORMAT = (
    'Output two zones. First a line "FINDINGS" then one bullet per check tagged '
    '[verified]. Then a line beginning "VERDICT:" followed by <GO|'
    'GO-WITH-CONDITIONS|NO-GO|INSUFFICIENT-DATA> and these headers each on their '
    'own line: CONFIDENCE:, IMPACTED SERVICES / COMPONENTS:, PACKET-FLOW:, '
    'REASONING:, CONDITIONS:, ROLLBACK:, RESIDUAL-UNKNOWNS:. Plain language '
    'only — no internal check names, no "Batfish", no snapshot IDs.'
)


def _openai_tools() -> list[dict]:
    """Translator tool defs in OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TRANSLATOR_TOOLS
    ]


# --------------------------------------------------------------------------
# Ledger (docs/04) — LLM-adjacent state the app holds across turns
# --------------------------------------------------------------------------
@dataclass
class Ledger:
    network: str = "netguard"
    base: str | None = None
    chain: list[str] = field(default_factory=list)
    current: str | None = None
    edits: list[str] = field(default_factory=list)          # human-readable deltas
    configs: dict[str, str] = field(default_factory=dict)    # current edited set
    original_configs: dict[str, str] = field(default_factory=dict)
    device_summary: str = ""                                 # from snapshot_info
    # docs/04 Case A (via pybatfish fork_snapshot): the accumulated failure set,
    # applied as ONE fork on top of the latest edit snapshot.
    node_failures: list[str] = field(default_factory=list)
    interface_failures: list[str] = field(default_factory=list)
    edit_snapshot: str | None = None   # latest re-staged (config-edit) snapshot
    _fork_seq: int = 0

    @property
    def previous(self) -> str | None:
        """Snapshot before `current` (diff target for 'what did THIS change break')."""
        seq = [self.base] + self.chain
        if self.current in seq:
            i = seq.index(self.current)
            return seq[i - 1] if i > 0 else None
        return self.base

    def to_public_dict(self) -> dict:
        return {
            "network": self.network,
            "base": self.base,
            "chain": self.chain,
            "current": self.current,
            "edits": self.edits,
            "failure_set": {"nodes": self.node_failures,
                            "interfaces": self.interface_failures},
        }

    def apply_failure_fork(self, engine: DirectEngine) -> str:
        """Fork the latest edit snapshot with the WHOLE failure set -> current."""
        base = self.edit_snapshot or self.base
        self._fork_seq += 1
        name = f"fail{self._fork_seq}"
        engine.fork_with_failures(self.network, base, name,
                                  self.node_failures, self.interface_failures)
        self.chain.append(name)
        self.current = name
        return name


# --------------------------------------------------------------------------
# Upload turn (docs/02) — deterministic, no LLM
# --------------------------------------------------------------------------
def run_upload_turn(ops: BatfishOps, ledger: Ledger,
                    configs: dict[str, str]) -> tuple[bool, str]:
    """init base snapshot -> parse-check GATE -> snapshot info. Returns (ok, msg)."""
    try:
        ops.init_snapshot(ledger.network, "base", configs)
    except MCPToolError as e:
        return False, f"Snapshot init failed: {e}"

    try:
        ps = ops.parse_status(ledger.network, "base")
    except MCPToolError as e:
        return False, f"Parse-status check failed: {e}"
    errors = ps.get("errors") or [] if isinstance(ps, dict) else []
    warnings = ps.get("warnings") or [] if isinstance(ps, dict) else []
    if errors:
        return False, (
            "Parse FAILED — a verdict on a half-parsed config is worse than no "
            f"verdict. Errors: {json.dumps(errors)[:2000]}"
        )

    try:
        info = ops.snapshot_info(ledger.network, "base")
    except MCPToolError as e:
        return False, f"Snapshot info failed: {e}"

    nodes = info.get("nodes", []) if isinstance(info, dict) else []
    interfaces = info.get("interfaces", []) if isinstance(info, dict) else []
    vendors = info.get("vendors", []) if isinstance(info, dict) else []

    ledger.base = "base"
    ledger.current = "base"
    ledger.chain = []
    ledger.edits = []
    ledger.edit_snapshot = "base"
    ledger.node_failures = []
    ledger.interface_failures = []
    ledger._fork_seq = 0

    # Hygiene: engine snapshots outlive app sessions — drop stale failN/changeN
    # from earlier sessions so engine state matches this fresh ledger.
    try:
        listed = ops.list_snapshots(ledger.network)
        for snap in (listed.get("snapshots") or []) if isinstance(listed, dict) else []:
            if snap != "base":
                ops.delete_snapshot(ledger.network, snap)
    except MCPToolError:
        pass
    ledger.configs = dict(configs)
    ledger.original_configs = dict(configs)
    ledger.device_summary = json.dumps(
        {"nodes": nodes, "vendors": vendors, "interfaces": interfaces}
    )

    msg = (
        f"Network loaded: {len(nodes)} devices ({', '.join(vendors)}), all "
        f"configs parsed cleanly"
        + (f" ({len(warnings)} warnings)" if warnings else "")
        + ". Ask me a what-if scenario."
    )
    return True, msg


# --------------------------------------------------------------------------
# Translator tool definitions (Anthropic tool-use schema)
# --------------------------------------------------------------------------
def _snapshot_prop():
    return {
        "type": "string",
        "description": "Snapshot to query. Omit for the ledger's `current`. "
                       "Use 'base' or a changeN name to compare/diff.",
    }


TRANSLATOR_TOOLS = [
    {
        "name": "apply_failure_set",
        "description": (
            "THE way to simulate failures — REQUIRED FIRST CALL for any scenario "
            "where the user fails/shuts/downs a node, link, or interface, including "
            "stacked failures ('now also fail X'). Adds the failures to the "
            "session's cumulative failure set, forks the latest edit snapshot with "
            "the WHOLE set deactivated (one cheap engine fork), advances the "
            "ledger's current snapshot, and updates the UI ledger + topology "
            "diagram. All subsequent probes (traceroute etc.) then run against the "
            "failed snapshot by default. Use stage_change_snapshot only for "
            "policy/config EDITS (route-maps, ACLs, neighbors)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "node_failures": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Whole devices to fail, e.g. ['london']",
                },
                "interface_failures": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Interfaces to fail as node[iface], e.g. "
                                   "['as1border1[GigabitEthernet1/0]']",
                },
                "reset": {
                    "type": "boolean",
                    "description": "true = clear the previous failure set first "
                                   "(scenario is NOT stacking on earlier failures)",
                },
            },
        },
    },
    {
        "name": "batfish_failure_impact",
        "description": (
            "OPTIONAL supplementary check, never a substitute for "
            "apply_failure_set: read-only coarse diff of the auto-generated "
            "reachability matrix for a SINGLE failure. It does NOT record the "
            "failure in the session ledger and does NOT create a snapshot you can "
            "probe afterwards — call apply_failure_set FIRST, then use this (against "
            "the pre-failure snapshot) only if you want the broad blast-radius "
            "matrix as extra evidence. NO_IMPACT means no previously-working flow "
            "broke; it does not mean the topology is unchanged."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "failure_type": {"type": "string", "enum": ["node", "interface"]},
                "target": {
                    "type": "string",
                    "description": "Node name, or interface as node[interface], "
                                   "e.g. as1border1[GigabitEthernet1/0]",
                },
                "snapshot": _snapshot_prop(),
            },
            "required": ["failure_type", "target"],
        },
    },
    {
        "name": "batfish_check_routing",
        "description": (
            "Routing-process HEALTH CHECK: confirms BGP/OSPF process configuration "
            "presence per device (PASS/FAIL + process table). It does NOT return "
            "session status or best paths — never claim session state from this."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "protocols": {
                    "type": "array", "items": {"type": "string", "enum": ["bgp", "ospf"]},
                },
                "snapshot": _snapshot_prop(),
            },
            "required": ["protocols"],
        },
    },
    {
        "name": "batfish_simulate_traffic",
        "description": (
            "Reachability for a described flow: does traffic from src get to dst, "
            "and how is it disposed (accepted/denied/no-route). dst must be a NODE "
            "NAME or IP that exists in the model (a bare prefix like 2.128.0.0/16 "
            "fails — use a host IP inside it, e.g. 2.128.0.1, with "
            "network_traceroute instead). Same VANTAGE RULE as network_traceroute: "
            "probe from an interior device, not only the device you just changed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "Source node/location/IP"},
                "dst": {"type": "string", "description": "Destination node/location/IP"},
                "applications": {"type": "array", "items": {"type": "string"},
                                  "description": "Optional, e.g. ['ssh','http']"},
                "snapshot": _snapshot_prop(),
            },
            "required": ["src", "dst"],
        },
    },
    {
        "name": "network_traceroute",
        "description": (
            "Hop-by-hop path trace from a source location to a destination IP: "
            "routing decisions, ACL evaluations, final disposition. VANTAGE RULE: "
            "after failing/shutting anything on device X, never judge reachability "
            "from X alone — X's own view is usually the outlier. Run the same "
            "traceroute from at least one interior device behind X (e.g. a core "
            "router) before concluding anything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_location": {"type": "string",
                                     "description": "Node name, interface, or IP"},
                "dest_ip": {"type": "string"},
                "dest_port": {"type": "integer"},
                "ip_protocol": {"type": "string", "enum": ["tcp", "udp", "icmp"]},
                "src_ip": {"type": "string"},
                "snapshot": _snapshot_prop(),
            },
            "required": ["source_location", "dest_ip"],
        },
    },
    {
        "name": "network_bidirectional_reachability",
        "description": "Both-direction reachability between two locations — catches "
                       "asymmetric routing and one-way blocks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location_a": {"type": "string"},
                "location_b": {"type": "string"},
                "ip_a": {"type": "string"},
                "ip_b": {"type": "string"},
                "port": {"type": "integer"},
                "protocol": {"type": "string", "enum": ["tcp", "udp", "icmp"]},
                "snapshot": _snapshot_prop(),
            },
            "required": ["location_a", "location_b", "ip_a", "ip_b"],
        },
    },
    {
        "name": "network_analyze_acl_rules",
        "description": "ACL/filter behavior analysis: matching lines, shadowed rules, "
                       "reachability of filter lines. Optionally scope to one ACL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "acl_name": {"type": "string"},
                "snapshot": _snapshot_prop(),
            },
        },
    },
    {
        "name": "get_snapshot_info",
        "description": "Device/interface inventory and model summary for a snapshot.",
        "input_schema": {
            "type": "object",
            "properties": {"snapshot": _snapshot_prop()},
        },
    },
    {
        "name": "bgp_session_status",
        "description": (
            "REAL BGP session establishment status per neighbor (ESTABLISHED / "
            "NOT_ESTABLISHED / NOT_COMPATIBLE) plus a summary count. Use this — "
            "not batfish_check_routing — for any claim about sessions. Compare "
            "across snapshots to see sessions a change tears down."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"snapshot": _snapshot_prop()},
        },
    },
    {
        "name": "differential_reachability",
        "description": (
            "ENGINE-NATIVE DIFF of reachability between two snapshots: returns "
            "exactly the flows whose disposition changed (what the change broke "
            "or fixed). This is the authoritative 'what did THIS change break' "
            "answer — prefer it over hand-comparing traceroutes. reference = "
            "before, snapshot = after."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "snapshot": {"type": "string",
                              "description": "AFTER snapshot (default: current)"},
                "reference_snapshot": {
                    "type": "string",
                    "description": "BEFORE snapshot (default: the one before "
                                   "current, else base)",
                },
            },
        },
    },
    {
        "name": "detect_loops",
        "description": "Engine check for forwarding loops in a snapshot. Run on "
                       "the changed snapshot before any GO verdict.",
        "input_schema": {
            "type": "object",
            "properties": {"snapshot": _snapshot_prop()},
        },
    },
    {
        "name": "health_checks",
        "description": (
            "Config-health bundle for a snapshot: parse/init issues, undefined "
            "references, duplicate interface IPs across devices, BGP session "
            "summary (incl. dead/incompatible peers), forwarding loops. Use for "
            "'any problems with the current configs?' style questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"snapshot": _snapshot_prop()},
        },
    },
    {
        "name": "routes_to",
        "description": "Main-RIB routes matching a prefix (optionally scoped to "
                       "nodes) — shows what actually got SELECTED into the RIB.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prefix": {"type": "string", "description": "e.g. 2.128.0.0/16"},
                "nodes": {"type": "string",
                           "description": "optional node regex/specifier"},
                "snapshot": _snapshot_prop(),
            },
            "required": ["prefix"],
        },
    },
    {
        "name": "differential_query",
        "description": (
            "Native engine diff of ONE table question between the base snapshot "
            "and the current changed/failed snapshot — returns only the rows that "
            "changed. Use to show exactly what an edit or failure altered for a "
            "specific class of fact. `question` is one of: routes, bgpRib, "
            "bgpSessionStatus, bgpPeerConfiguration, bgpEdges, interfaceProperties, "
            "nodeProperties, definedStructures, undefinedReferences, ospfEdges, "
            "edges. Optional `question_args` scope it, e.g. {\"network\": "
            "\"2.128.0.0/16\"} for routes or {\"nodes\": \"/as1/\"}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string",
                              "description": "diffable question name (see list above)"},
                "question_args": {"type": "object",
                                   "description": "optional kwargs passed to the question"},
                "reference_snapshot": {"type": "string",
                                        "description": "before-snapshot (default: base)"},
                "snapshot": _snapshot_prop(),
            },
            "required": ["question"],
        },
    },
    {
        "name": "test_route_policy",
        "description": (
            "Evaluate how a route-map / routing policy processes a SPECIFIC BGP "
            "route announcement: returns PERMIT or DENY, the modified output "
            "route (communities, local-pref, metric …), and the matched clause. "
            "Use for 'how is prefix X treated by this policy?' and to check a "
            "route-map edit's effect. input_route needs at least {\"network\": "
            "\"<prefix>\"}; optional asPath (list of ASNs), communities (list), "
            "localPreference, metric. direction is 'in' or 'out'. Optionally "
            "scope with policies (name/regex) and nodes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "input_route": {"type": "object",
                                 "description": "announcement, e.g. {\"network\": "
                                 "\"10.0.0.0/24\", \"asPath\": [65001]}"},
                "direction": {"type": "string", "enum": ["in", "out"]},
                "policies": {"type": "string",
                              "description": "optional policy name/regex"},
                "nodes": {"type": "string",
                           "description": "optional node specifier"},
                "snapshot": _snapshot_prop(),
            },
            "required": ["input_route", "direction"],
        },
    },
    {
        "name": "search_route_policy",
        "description": (
            "Exhaustively search for route announcements a policy treats with a "
            "given action ('permit' or 'deny') — a counterexample proof, not a "
            "sample. Search 'deny' over the space you INTEND to permit: any "
            "result is a route the policy wrongly drops; EMPTY means the intent "
            "holds for the whole space. input_constraints / output_constraints "
            "narrow the search (prefix, communities, asPath, localPreference, "
            "med, complementPrefix)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["permit", "deny"]},
                "input_constraints": {"type": "object",
                                       "description": "e.g. {\"prefix\": "
                                       "[\"10.0.0.0/8:8-32\"]}"},
                "output_constraints": {"type": "object"},
                "policies": {"type": "string"},
                "nodes": {"type": "string"},
                "snapshot": _snapshot_prop(),
            },
            "required": ["action"],
        },
    },
    {
        "name": "test_filter",
        "description": (
            "Deterministically evaluate whether an ACL/filter PERMITs or DENYs a "
            "SPECIFIC flow, returning the matched line. headers describe the flow "
            "space: srcIps, dstIps (CIDR or IP), dstPorts, srcPorts, ipProtocols "
            "(e.g. [\"tcp\"]), applications (e.g. [\"ssh\"]). Optionally scope with "
            "filters (ACL name/regex) and nodes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "headers": {"type": "object",
                             "description": "flow, e.g. {\"dstIps\": \"10.0.0.5\", "
                             "\"dstPorts\": \"22\", \"ipProtocols\": [\"tcp\"]}"},
                "filters": {"type": "string", "description": "optional ACL name/regex"},
                "nodes": {"type": "string", "description": "optional node specifier"},
                "start_location": {"type": "string"},
                "snapshot": _snapshot_prop(),
            },
            "required": ["headers"],
        },
    },
    {
        "name": "search_filter",
        "description": (
            "Search a filter's whole flow space for flows it treats with action "
            "('permit'|'deny') — a proof, not a sample. Provably-safe ACL change "
            "pattern: (1) search 'permit' for the intended traffic on base = "
            "confirm it is NOT already allowed; (2) after the edit, search 'deny' "
            "for the same traffic = EMPTY proves all intended flows now pass; "
            "(3) set invert_search=true to search OUTSIDE the intended header "
            "space for newly-permitted flows = the collateral-damage check."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "headers": {"type": "object", "description": "the intended flow space"},
                "action": {"type": "string", "enum": ["permit", "deny"]},
                "invert_search": {"type": "boolean",
                                   "description": "search outside the header space"},
                "filters": {"type": "string"},
                "nodes": {"type": "string"},
                "start_location": {"type": "string"},
                "snapshot": _snapshot_prop(),
            },
            "required": ["headers", "action"],
        },
    },
    {
        "name": "compare_filters",
        "description": (
            "Filter lines that treat some flow differently between the base and "
            "the changed snapshot — the authoritative 'what did this ACL edit "
            "change'. Empty means the edit altered no filter behavior."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reference_snapshot": {"type": "string",
                                        "description": "before-snapshot (default base)"},
                "filters": {"type": "string"},
                "nodes": {"type": "string"},
                "snapshot": _snapshot_prop(),
            },
        },
    },
    {
        "name": "filter_line_reachability",
        "description": (
            "Find ACL lines that can never match (shadowed/dead lines) — config "
            "hygiene. Empty is clean."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filters": {"type": "string"},
                "nodes": {"type": "string"},
                "snapshot": _snapshot_prop(),
            },
        },
    },
    {
        "name": "bgp_compatibility",
        "description": (
            "Configured BGP session COMPATIBILITY — why a peering will or won't "
            "come up at the config level (NO_LOCAL_IP, UNKNOWN_REMOTE, "
            "NO_MATCH_FOUND …). Complements bgp_session_status (which reports "
            "established state). Use when a change touches BGP peering/addressing "
            "and you need to know why a session breaks. Optional nodes / "
            "remote_nodes scope."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nodes": {"type": "string"},
                "remote_nodes": {"type": "string"},
                "snapshot": _snapshot_prop(),
            },
        },
    },
    {
        "name": "bgp_rib",
        "description": (
            "Routes in the BGP RIB (learned via BGP, before best-path selection) "
            "— richer than routes_to when the question is specifically about BGP "
            "advertisement/receipt. Optional nodes and prefix (network) scope."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nodes": {"type": "string"},
                "prefix": {"type": "string", "description": "e.g. 2.128.0.0/16"},
                "snapshot": _snapshot_prop(),
            },
        },
    },
    {
        "name": "bgp_edges",
        "description": "Established BGP adjacencies (who peers with whom). "
                       "Optional nodes / remote_nodes scope.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nodes": {"type": "string"},
                "remote_nodes": {"type": "string"},
                "snapshot": _snapshot_prop(),
            },
        },
    },
    {
        "name": "prefix_tracer",
        "description": (
            "Trace how a prefix propagates (originated / received / advertised / "
            "installed) across the network — answers 'does this prefix still "
            "reach device X after the change?'. Requires prefix; optional nodes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prefix": {"type": "string", "description": "e.g. 2.128.0.0/16"},
                "nodes": {"type": "string"},
                "snapshot": _snapshot_prop(),
            },
            "required": ["prefix"],
        },
    },
    {
        "name": "ospf_compatibility",
        "description": (
            "OSPF adjacency compatibility — incompatible or unestablished OSPF "
            "neighbor pairs and why (area / network-type / MTU / timer mismatch). "
            "Use when a change touches OSPF interfaces/areas. Optional nodes / "
            "remote_nodes scope."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nodes": {"type": "string"},
                "remote_nodes": {"type": "string"},
                "snapshot": _snapshot_prop(),
            },
        },
    },
    {
        "name": "ospf_edges",
        "description": "Established OSPF adjacencies. Optional nodes / "
                       "remote_nodes scope.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nodes": {"type": "string"},
                "remote_nodes": {"type": "string"},
                "snapshot": _snapshot_prop(),
            },
        },
    },
    {
        "name": "ospf_process_config",
        "description": "OSPF process configuration (router-id, areas, reference "
                       "bandwidth). Optional nodes scope.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nodes": {"type": "string"},
                "snapshot": _snapshot_prop(),
            },
        },
    },
    {
        "name": "multipath_consistency",
        "description": (
            "Check equal-cost multipath (ECMP) consistency across the network: "
            "flows whose parallel paths disagree on delivery (some accepted, "
            "some dropped) — asymmetric forwarding that intermittently breaks "
            "traffic. Run before a GO when a change could alter path diversity. "
            "Empty is clean."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"snapshot": _snapshot_prop()},
        },
    },
    {
        "name": "read_config",
        "description": "Read the CURRENT (post-edits) config text for one device "
                       "file, so edits can be expressed precisely.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "e.g. as1border1.cfg"},
            },
            "required": ["filename"],
        },
    },
    {
        "name": "stage_change_snapshot",
        "description": (
            "RE-STAGE approach (docs/04): apply config edits and build a new snapshot "
            "layered on the ledger's current state. Provide the FULL new text of each "
            "changed file (read_config first; keep edits minimal and precise) plus a "
            "one-line edit_summary echoing the exact delta. The app merges the files "
            "into the cumulative config set, builds changeN, runs the parse gate, and "
            "advances the ledger (current -> changeN). Use for policy/config edits AND "
            "for stacked failures (interface shutdown edits)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "edited_configs": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "{filename: full new config text} for changed files only",
                },
                "edit_summary": {
                    "type": "string",
                    "description": "Exact delta, e.g. \"as1border1 Gi1/0: add 'shutdown'\"",
                },
            },
            "required": ["edited_configs", "edit_summary"],
        },
    },
]


def _truncate(obj) -> str:
    # No cap — the worker has a large context window; return results in full.
    return obj if isinstance(obj, str) else json.dumps(obj, default=str)


_ENGINE = DirectEngine()

_IFACE_RE = re.compile(r"^(\S+?)[\s\[]+(.+?)\]?$")


def _normalize_iface(spec: str) -> str:
    """Accept 'node[Iface]' or the looser 'node Iface' some models emit and
    return the canonical 'node[Iface]' the engine expects."""
    spec = spec.strip()
    if "[" in spec and spec.endswith("]"):
        return spec
    m = _IFACE_RE.match(spec)
    return f"{m.group(1)}[{m.group(2).strip()}]" if m else spec


_NODE_REGEX_META = re.compile(r"[\\^$()|\[\]*+?{}]")


def _normalize_node_spec(spec):
    """Coerce a model-supplied node specifier into valid Batfish grammar.

    Batfish node specifiers accept a bare name, a comma-separated set, or a
    regex wrapped in /.../. Models routinely emit a raw anchored regex like
    '^(border1|border2)$', which the parboiled parser rejects outright (and
    the failure previously tore down the whole turn). Wrap anything that looks
    like a regex in slashes; since Batfish matches regexes with find(), drop a
    redundant leading ^ / trailing $ so the common substring intent survives
    (e.g. '^(border1|border2)$' -> '/(border1|border2)/', which matches
    as1border1/as1border2)."""
    if spec is None:
        return None
    s = str(spec).strip()
    if not s:
        return None
    if len(s) > 1 and s.startswith("/") and s.endswith("/"):
        return s  # already a regex literal
    if _NODE_REGEX_META.search(s):
        if s.startswith("^"):
            s = s[1:]
        if s.endswith("$"):
            s = s[:-1]
        return f"/{s}/"
    return s  # bare name or comma-separated set — valid as-is


def _coerce_failure_args(args: dict) -> dict:
    """Tolerate the loose failure-set arg shapes these models emit and coerce to
    {node_failures, interface_failures}. Handles the schema shape plus common
    variants: a `failures`/`failure_set` list of {type,node,interface} objects
    or "node[iface]" strings, and singular target/node/interface keys."""
    nf = list(args.get("node_failures") or [])
    ifc = list(args.get("interface_failures") or [])

    def add(item):
        if isinstance(item, str):
            (ifc if "[" in item or " " in item.strip() else nf).append(item)
        elif isinstance(item, dict):
            node = item.get("node") or item.get("hostname") or item.get("device")
            iface = item.get("interface") or item.get("iface")
            typ = str(item.get("type", "")).lower()
            if iface and node:
                ifc.append(f"{node}[{iface}]")
            elif node and ("node" in typ or typ in ("", "node_down", "node")):
                nf.append(node)
            elif node:
                nf.append(node)

    for key in ("failures", "failure_set", "targets", "items"):
        v = args.get(key)
        if isinstance(v, list):
            for it in v:
                add(it)
        elif isinstance(v, (str, dict)):
            add(v)
    # singular convenience keys — a split node+interface is ONE interface
    # failure, not also a standalone node failure.
    if args.get("interface"):
        n = args.get("node") or args.get("hostname") or args.get("device") or ""
        ifc.append(f"{n}[{args['interface']}]" if n else args["interface"])
    else:
        for k in ("target", "node", "device", "hostname"):
            if args.get(k):
                add(args[k])

    out = dict(args)
    out["node_failures"] = nf
    out["interface_failures"] = ifc
    return out


def _execute_translator_tool(ops: BatfishOps, ledger: Ledger,
                             name: str, args: dict) -> str:
    """Map a translator tool call onto MCP calls, direct-engine questions, and
    app-side ledger ops."""
    net = ledger.network
    snap = args.get("snapshot") or ledger.current

    # ---- direct-engine tools (pybatfish) ----------------------------------
    if name == "apply_failure_set":
        args = _coerce_failure_args(args)
        if args.get("reset"):
            ledger.node_failures = []
            ledger.interface_failures = []
        for n in args.get("node_failures") or []:
            if n not in ledger.node_failures:
                ledger.node_failures.append(n)
        for i in args.get("interface_failures") or []:
            i = _normalize_iface(i)
            if i not in ledger.interface_failures:
                ledger.interface_failures.append(i)
        if not (ledger.node_failures or ledger.interface_failures):
            return "ERROR: failure set is empty — nothing to apply"
        fork = ledger.apply_failure_fork(_ENGINE)
        ledger.edits.append(
            "failure set -> nodes=" + json.dumps(ledger.node_failures)
            + " interfaces=" + json.dumps(ledger.interface_failures)
        )
        return json.dumps({
            "ok": True, "snapshot": fork,
            "active_failure_set": {"nodes": ledger.node_failures,
                                    "interfaces": ledger.interface_failures},
            "ledger": ledger.to_public_dict(),
        })
    if name == "bgp_session_status":
        return _truncate(_ENGINE.bgp_sessions(net, snap))
    if name == "differential_reachability":
        after = args.get("snapshot") or ledger.current
        before = args.get("reference_snapshot") or ledger.previous or ledger.base
        if after == before:
            return "ERROR: snapshot and reference_snapshot are both " + str(after)
        out = _ENGINE.differential_reachability(net, after, before)
        out["compared"] = {"before": before, "after": after}
        return _truncate(out)
    if name == "differential_query":
        after = args.get("snapshot") or ledger.current
        before = args.get("reference_snapshot") or ledger.base
        if after == before:
            return ("ERROR: nothing to diff — the current snapshot equals the "
                    "reference; apply a change/failure first")
        if args.get("question") not in _ENGINE.DIFFABLE_QUESTIONS:
            return ("ERROR: question must be one of "
                    + json.dumps(sorted(_ENGINE.DIFFABLE_QUESTIONS)))
        return _truncate(_ENGINE.diff(net, before, after,
                                      question=args["question"],
                                      question_args=args.get("question_args")))
    if name == "test_route_policy":
        return _truncate(_ENGINE.test_route_policy(
            net, snap, args["input_route"], args["direction"],
            policies=args.get("policies"),
            nodes=_normalize_node_spec(args.get("nodes"))))
    if name == "search_route_policy":
        return _truncate(_ENGINE.search_route_policy(
            net, snap, args["action"],
            input_constraints=args.get("input_constraints"),
            output_constraints=args.get("output_constraints"),
            policies=args.get("policies"),
            nodes=_normalize_node_spec(args.get("nodes"))))
    if name == "test_filter":
        return _truncate(_ENGINE.test_filter(
            net, snap, args["headers"], filters=args.get("filters"),
            nodes=_normalize_node_spec(args.get("nodes")),
            start_location=args.get("start_location")))
    if name == "search_filter":
        return _truncate(_ENGINE.search_filter(
            net, snap, args["headers"], args["action"],
            invert_search=bool(args.get("invert_search")),
            filters=args.get("filters"),
            nodes=_normalize_node_spec(args.get("nodes")),
            start_location=args.get("start_location")))
    if name == "compare_filters":
        before = args.get("reference_snapshot") or ledger.base
        if snap == before:
            return ("ERROR: nothing to compare — current snapshot equals the "
                    "reference; apply a config edit first")
        return _truncate(_ENGINE.compare_filters(
            net, before, snap, filters=args.get("filters"),
            nodes=_normalize_node_spec(args.get("nodes"))))
    if name == "filter_line_reachability":
        return _truncate(_ENGINE.filter_line_reachability(
            net, snap, filters=args.get("filters"),
            nodes=_normalize_node_spec(args.get("nodes"))))
    if name == "bgp_compatibility":
        return _truncate(_ENGINE.bgp_compatibility(
            net, snap, nodes=_normalize_node_spec(args.get("nodes")),
            remote_nodes=_normalize_node_spec(args.get("remote_nodes"))))
    if name == "bgp_rib":
        return _truncate(_ENGINE.bgp_rib(
            net, snap, nodes=_normalize_node_spec(args.get("nodes")),
            prefix=args.get("prefix")))
    if name == "bgp_edges":
        return _truncate(_ENGINE.bgp_edges(
            net, snap, nodes=_normalize_node_spec(args.get("nodes")),
            remote_nodes=_normalize_node_spec(args.get("remote_nodes"))))
    if name == "prefix_tracer":
        return _truncate(_ENGINE.prefix_tracer(
            net, snap, args["prefix"],
            nodes=_normalize_node_spec(args.get("nodes"))))
    if name == "ospf_compatibility":
        return _truncate(_ENGINE.ospf_compatibility(
            net, snap, nodes=_normalize_node_spec(args.get("nodes")),
            remote_nodes=_normalize_node_spec(args.get("remote_nodes"))))
    if name == "ospf_edges":
        return _truncate(_ENGINE.ospf_edges(
            net, snap, nodes=_normalize_node_spec(args.get("nodes")),
            remote_nodes=_normalize_node_spec(args.get("remote_nodes"))))
    if name == "ospf_process_config":
        return _truncate(_ENGINE.ospf_process_config(
            net, snap, nodes=_normalize_node_spec(args.get("nodes"))))
    if name == "detect_loops":
        return _truncate(_ENGINE.detect_loops(net, snap))
    if name == "multipath_consistency":
        return _truncate(_ENGINE.multipath_consistency(net, snap))
    if name == "health_checks":
        return _truncate(_ENGINE.health_checks(net, snap))
    if name == "routes_to":
        return _truncate(_ENGINE.routes_to(net, snap, args["prefix"],
                                           _normalize_node_spec(args.get("nodes"))))

    if name == "batfish_failure_impact":
        return _truncate(ops.failure_impact(net, snap, args["failure_type"], args["target"]))
    if name == "batfish_check_routing":
        return _truncate(ops.check_routing(net, snap, args["protocols"]))
    if name == "batfish_simulate_traffic":
        return _truncate(ops.simulate_traffic(net, snap, args["src"], args["dst"],
                                              args.get("applications")))
    if name == "network_traceroute":
        return _truncate(ops.traceroute(net, snap, args["source_location"],
                                        args["dest_ip"], args.get("dest_port"),
                                        args.get("ip_protocol", "tcp"),
                                        args.get("src_ip")))
    if name == "network_bidirectional_reachability":
        return _truncate(ops.bidir_reach(net, snap, args["location_a"], args["location_b"],
                                         args["ip_a"], args["ip_b"],
                                         args.get("port"), args.get("protocol", "tcp")))
    if name == "network_analyze_acl_rules":
        return _truncate(ops.analyze_acl(net, snap, args.get("acl_name")))
    if name == "get_snapshot_info":
        return _truncate(ops.snapshot_info(net, snap))
    if name == "read_config":
        fn = args["filename"]
        if fn not in ledger.configs:
            return f"ERROR: no such config file: {fn}. Files: {sorted(ledger.configs)}"
        return ledger.configs[fn]
    if name == "stage_change_snapshot":
        edited = args["edited_configs"]
        unknown = [f for f in edited if f not in ledger.configs]
        if unknown:
            return f"ERROR: unknown config file(s): {unknown}"
        # Guard against fragment-instead-of-full-file edits: a config that
        # shrinks drastically almost certainly lost everything but the delta,
        # which silently guts the device in the model (wrong question answered
        # confidently — the worst outcome).
        for fn, text in edited.items():
            old_len = len(ledger.configs[fn])
            if len(text) < 0.5 * old_len:
                return (
                    f"ERROR: {fn} shrank from {old_len} to {len(text)} chars — you "
                    "must supply the COMPLETE file with your minimal edit applied, "
                    "not just the changed stanza. Call read_config, apply the edit "
                    "to the full text, and retry. Ledger NOT advanced."
                )
        new_configs = dict(ledger.configs)
        new_configs.update(edited)
        new_name = f"change{len(ledger.chain) + 1}"
        ops.init_snapshot(net, new_name, new_configs)
        ps = ops.parse_status(net, new_name)
        errors = ps.get("errors") or [] if isinstance(ps, dict) else []
        if errors:
            # do NOT advance the ledger on a broken edit
            return ("ERROR: edited configs failed to parse; ledger NOT advanced. "
                    f"Errors: {json.dumps(errors)[:2000]}")
        ledger.configs = new_configs
        ledger.chain.append(new_name)
        ledger.current = new_name
        ledger.edit_snapshot = new_name
        ledger.edits.append(args["edit_summary"])
        # Edits and failures compose: if a failure set is active, re-apply it
        # as a fork on top of the new edit snapshot.
        refork = None
        if ledger.node_failures or ledger.interface_failures:
            refork = ledger.apply_failure_fork(_ENGINE)
        return json.dumps({
            "ok": True, "snapshot": refork or new_name, "parse": "clean",
            "note": (f"active failure set re-applied on top of {new_name} "
                     f"as {refork}" if refork else None),
            "ledger": ledger.to_public_dict(),
        })
    return f"ERROR: unknown tool {name}"


# --------------------------------------------------------------------------
# Scenario turn (docs/02): translate -> compute -> synthesize
# --------------------------------------------------------------------------
# The worker owns all behavioral instructions (persona + domain rules + output
# formats + vendor reference) in its system prompt. The app sends only DATA:
# the ledger, the device inventory, the check catalog, the question, and the
# results. See Misc/docs/08 for the worker system prompt.

def _translator_context(ledger: Ledger) -> str:
    """DATA the translator needs — session state + device inventory (no
    behavioral instructions; those live in the worker prompt)."""
    return (
        "## Current session state (ledger)\n"
        f"{json.dumps(ledger.to_public_dict(), indent=1)}\n\n"
        "## Device inventory (nodes / interfaces / vendors)\n"
        f"{ledger.device_summary}"
    )


def _run_verifier(provider, ledger: Ledger, user_text: str, engine_facts: str,
                  notify) -> dict | None:
    """Independent completeness/soundness review. Returns the parsed JSON dict,
    or None if the verifier could not be run or parsed (never fatal — the
    deterministic guards are the hard floor)."""
    notify("Verifying investigation")
    data = (f"## USER QUESTION\n{user_text}\n\n## SESSION STATE\n"
            f"{json.dumps(ledger.to_public_dict())}\n\n"
            f"## CHECKS RUN AND RESULTS\n{engine_facts}")
    try:
        reply = provider.chat([{"role": "user", "content": data}], role="VERIFIER", format_hint=_VERIFIER_FORMAT)
    except Exception:
        return None
    text = reply.content.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) and "complete" in obj else None
    except (json.JSONDecodeError, ValueError):
        return None


_CHANGE_TOOLS = {"apply_failure_set", "stage_change_snapshot"}
_PROBE_TOOLS = {"network_traceroute", "batfish_simulate_traffic",
                "network_bidirectional_reachability"}


def _needs_probe_nudge(tool_log: list[dict]) -> bool:
    """A change/failure was applied but no reachability probe confirmed any
    specific flow — the investigation is incomplete regardless of what the
    model thinks."""
    ran = {e["tool"] for e in tool_log}
    return bool(ran & _CHANGE_TOOLS) and not (ran & _PROBE_TOOLS)


@dataclass
class ScenarioResult:
    findings: str
    verdict: str
    tool_log: list[dict]
    clarification: str | None = None   # set when the translator asked a question


def _translator_rounds(provider, ops: BatfishOps, ledger: Ledger,
                       messages: list[dict], tool_log: list[dict],
                       notify) -> str:
    """Run the translator tool-loop until it stops requesting checks. Mutates
    `messages` and `tool_log` in place; returns the final plain-text reply."""
    for _ in range(MAX_TRANSLATOR_ITERATIONS):
        reply = provider.chat(messages, tools=_openai_tools(),
                              role="TRANSLATOR", format_hint=_TRANSLATOR_FORMAT)
        if not reply.tool_calls:
            return reply.content
        messages.append({
            "role": "assistant", "content": reply.content,
            "tool_calls": [{"id": tc.id, "type": "function",
                             "function": {"name": tc.name,
                                          "arguments": json.dumps(tc.arguments)}}
                            for tc in reply.tool_calls]})
        for tc in reply.tool_calls:
            notify(f"Running: {FRIENDLY_CHECK.get(tc.name, tc.name.replace('_', ' '))}")
            try:
                out = _execute_translator_tool(ops, ledger, tc.name, tc.arguments)
                is_error = out.startswith("ERROR")
            except MCPToolError as e:
                out, is_error = f"ERROR: {e}", True
            except Exception as e:
                # Any engine/tool failure (e.g. a malformed specifier the model
                # supplied) is fed back as an ERROR result so the translator can
                # correct itself — it must never tear down the whole turn.
                out = f"ERROR: {tc.name} failed: {type(e).__name__}: {str(e)[:400]}"
                is_error = True
            tool_log.append({"tool": tc.name, "input": tc.arguments,
                             "result": out, "is_error": is_error})
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": out})
    return ""


def run_scenario_turn(ops: BatfishOps, ledger: Ledger, user_text: str,
                      chat_history: list[dict] | None = None,
                      progress=None) -> ScenarioResult:
    """Full scenario turn. `progress` is an optional callable(str) for UI status."""
    notify = progress or (lambda _msg: None)
    # fresh provider per turn -> fallback stickiness resets each turn
    provider = build_provider(on_switch=notify)

    # ---- translator investigation ---------------------------------------
    messages = (
        [{"role": "system", "content": _translator_context(ledger)}]
        + list(chat_history or [])
        + [{"role": "user", "content": user_text}]
    )
    tool_log: list[dict] = []
    final_text = _translator_rounds(provider, ops, ledger, messages, tool_log,
                                    notify)

    # Deterministic completeness guard (pre-verifier, one-shot): a change/failure
    # was applied but no reachability probe confirmed the specific flow.
    if tool_log and _needs_probe_nudge(tool_log):
        notify("Investigation incomplete — asking for probes")
        messages.append({"role": "user", "content": (
            "COMPLETENESS CHECK FAILED: you applied a change/failure but ran no "
            "reachability probe to confirm the specific service flow the user "
            "asked about. Run network_traceroute for that flow from an interior "
            "device (and differential_reachability + detect_loops if not already "
            "run). Then reply READY FOR SYNTHESIS.")})
        final_text = _translator_rounds(provider, ops, ledger, messages,
                                        tool_log, notify) or final_text

    # Ambiguity / insufficient-data path: no tools were run at all.
    if not tool_log and "READY FOR SYNTHESIS" not in final_text:
        return ScenarioResult("", "", tool_log, clarification=final_text or
                              "I couldn't classify that scenario — can you rephrase?")

    # ---- deterministic assertion floor (engine's own health gates) -------
    # If the scenario produced a changed/failed snapshot, run the parameterless
    # assertion gates on it. Only gates that REGRESS (newly fail vs base) are
    # attributable to this change and force NO-GO; pre-existing failures are
    # reported as facts but do not sink the verdict. This floor sits UNDER the
    # LLM verifier — the model may tighten it, never talk past it.
    gate_floor = ""
    if ledger.current and ledger.current != ledger.base:
        notify("Running: Health gates")
        try:
            after_g = _ENGINE.snapshot_gates(ledger.network, ledger.current)
            regressed: list[str] = []
            if after_g["gates_failed"]:
                before_g = _ENGINE.snapshot_gates(ledger.network, ledger.base)
                pre = {r["gate"] for r in before_g["results"]
                       if r["passed"] is False}
                regressed = [r["gate"] for r in after_g["results"]
                             if r["passed"] is False and r["gate"] not in pre]
            tool_log.append({
                "tool": "snapshot_gates",
                "input": {"after": ledger.current, "before": ledger.base},
                "result": json.dumps(
                    {**after_g, "regressed_gates": regressed,
                     "note": "regressed_gates newly fail on the change and are "
                             "attributable to it; any other failures pre-exist "
                             "on base and are not caused by this change"},
                    default=str),
                "is_error": bool(regressed)})
            if regressed:
                gate_floor = (
                    "\nGATE FLOOR: the engine's own health assertions newly FAIL "
                    f"on the changed snapshot ({', '.join(regressed)}). This "
                    "change breaks network health — the verdict MUST be NO-GO.\n")
        except Exception as e:  # gates are a floor, never a turn-killer
            notify(f"Health gates could not run ({type(e).__name__})")

    # ---- bounded verify -> remediate -> re-verify loop -------------------
    # The verifier is an adversarial second opinion. When it reports gaps, feed
    # the missing checks back to the translator and verify again — up to
    # MAX_VERIFY_CYCLES times. If still incomplete, proceed with the verifier's
    # recommended_floor (typically INSUFFICIENT-DATA). Deterministic guards
    # remain the hard floor underneath this.
    verifier_notes = None
    for cycle in range(MAX_VERIFY_CYCLES + 1):
        engine_facts = json.dumps(tool_log, indent=1, default=str)
        verifier_notes = _run_verifier(provider, ledger, user_text,
                                       engine_facts, notify)
        missing = (verifier_notes or {}).get("missing_probes")
        if not verifier_notes or not missing:
            break  # verifier satisfied (or unavailable) — done
        if cycle >= MAX_VERIFY_CYCLES:
            notify("Verifier still flags gaps after "
                   f"{MAX_VERIFY_CYCLES} cycles — proceeding with a capped verdict")
            break
        notify(f"Verifier flagged gaps (cycle {cycle + 1}/{MAX_VERIFY_CYCLES}) "
               "— gathering more evidence")
        messages.append({"role": "user", "content": (
            "VERIFIER found the investigation incomplete. Missing: "
            + json.dumps(missing) + ". Run exactly these checks now (e.g. "
            "traceroute from an interior device, differential_reachability, "
            "detect_loops as applicable), then reply READY FOR SYNTHESIS.")})
        _translator_rounds(provider, ops, ledger, messages, tool_log, notify)

    engine_facts = json.dumps(tool_log, indent=1, default=str)

    # ---- synthesizer -----------------------------------------------------
    notify("Synthesizing verdict")

    floor = gate_floor
    if verifier_notes and verifier_notes.get("recommended_floor"):
        floor += ("\nVERIFIER FLOOR: an independent review recommends the verdict "
                  f"be no better than {verifier_notes['recommended_floor']} — do "
                  "not exceed it unless the results clearly refute the concern.\n")

    synth_user = f"""## SCENARIO (user's words)
{user_text}

## SESSION STATE
{json.dumps(ledger.to_public_dict())}

## TRANSLATOR NOTE
{final_text or '(none)'}

## VERIFIER NOTES
{json.dumps(verifier_notes) if verifier_notes else '(none)'}
{floor}
## STRUCTURED CHECK RESULTS (the ONLY source of facts)
{engine_facts}
"""
    synth = provider.chat([{"role": "user", "content": synth_user}],
                          role="SYNTHESIZER",
                          format_hint=_SYNTHESIZER_FORMAT)
    # robust two-zone split (tolerant of markdown headers) + plain-language net
    findings, verdict = split_findings_verdict(strip_internal_terms(synth.content))
    return ScenarioResult(findings, verdict, tool_log)


# --------------------------------------------------------------------------
# Session cleanup (docs/04) — prune change snapshots on reset
# --------------------------------------------------------------------------
def cleanup_session(ops: BatfishOps, ledger: Ledger) -> None:
    for snap in ledger.chain:
        try:
            ops.delete_snapshot(ledger.network, snap)
        except MCPToolError:
            pass
