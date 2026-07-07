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
from dataclasses import dataclass, field
from pathlib import Path

from openai import OpenAI

from engine_direct import DirectEngine
from mcp_client import BatfishOps, MCPToolError
from ui_helpers import FRIENDLY_CHECK

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

# LLM backend: Ollama Cloud via its OpenAI-compatible endpoint.
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com/v1")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")
# Both confirmed tool-capable / accessible on this key (2026-07-06).
TRANSLATOR_MODEL = os.environ.get("NETGUARD_TRANSLATOR_MODEL", "qwen3-coder:480b")
SYNTHESIZER_MODEL = os.environ.get("NETGUARD_SYNTHESIZER_MODEL", "gpt-oss:120b")

MAX_RESULT_CHARS = 20_000  # cap a single tool result fed back to the LLM
MAX_TRANSLATOR_ITERATIONS = 12


def _llm() -> OpenAI:
    if not OLLAMA_API_KEY:
        raise RuntimeError("OLLAMA_API_KEY is not set (env or .env in repo root)")
    return OpenAI(base_url=OLLAMA_BASE_URL, api_key=OLLAMA_API_KEY)


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
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    if len(s) > MAX_RESULT_CHARS:
        s = s[:MAX_RESULT_CHARS] + f'... [truncated, {len(s)} chars total]'
    return s


_ENGINE = DirectEngine()


def _execute_translator_tool(ops: BatfishOps, ledger: Ledger,
                             name: str, args: dict) -> str:
    """Map a translator tool call onto MCP calls, direct-engine questions, and
    app-side ledger ops."""
    net = ledger.network
    snap = args.get("snapshot") or ledger.current

    # ---- direct-engine tools (pybatfish) ----------------------------------
    if name == "apply_failure_set":
        if args.get("reset"):
            ledger.node_failures = []
            ledger.interface_failures = []
        for n in args.get("node_failures") or []:
            if n not in ledger.node_failures:
                ledger.node_failures.append(n)
        for i in args.get("interface_failures") or []:
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
    if name == "detect_loops":
        return _truncate(_ENGINE.detect_loops(net, snap))
    if name == "health_checks":
        return _truncate(_ENGINE.health_checks(net, snap))
    if name == "routes_to":
        return _truncate(_ENGINE.routes_to(net, snap, args["prefix"],
                                           args.get("nodes")))

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
def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text()


def _translator_system(ledger: Ledger) -> str:
    base = _load_prompt("translator_system_prompt.md")
    addendum = f"""

## Runtime facts (this deployment — override anything above that conflicts)

- Tool names here are the REAL ones. `check_routing` returns protocol process
  presence ONLY — for session claims use `bgp_session_status`; for selected
  routes use `routes_to`; for reachability use traceroute / simulate_traffic /
  failure_impact / bidirectional tools.
- FAILURES (single or stacked): your FIRST tool call MUST be
  `apply_failure_set` whenever the user fails/shuts anything — it is the only
  action that records the failure in the session ledger and creates the failed
  snapshot your probes run against. Probing reachability without having
  applied the failure answers the WRONG question. "Now also fail X" = call it
  again with just X (the set persists). batfish_failure_impact is optional
  extra evidence only. Reserve stage_change_snapshot for policy/config EDITS;
  edits and the failure set compose automatically.
- DIFFS: `differential_reachability` is the authoritative "what did this
  change break" — run it (after=current, before=previous or base) for every
  change scenario, then confirm the specific service flow with traceroute.
- Before concluding GO on any change, run `detect_loops` on the changed
  snapshot.
- stage_change_snapshot applies edits and advances the ledger automatically.
  Call read_config first; supply full new file text; echo the exact delta in
  edit_summary.
- VANTAGE POINTS: after failing a link/interface on device X, do NOT judge
  reachability only from X itself — its local view is often the outlier. Probe
  from at least one interior device behind it (e.g. a core router) too. If
  failure_impact says NO_IMPACT but a single traceroute says NO_ROUTE, that is
  a vantage-point conflict: run more traceroutes from other sources before
  handing to synthesis, and report the per-source results.
- When your investigation is complete, STOP calling tools and write the single
  line `READY FOR SYNTHESIS` followed by a one-paragraph note on which tool
  results matter and what diff comparisons you made. Do NOT write the verdict.
- If the scenario is ambiguous, ask ONE clarifying question (no tools). If a
  required device/peer is missing from the model, say INSUFFICIENT-DATA and
  name the gap.

## Current ledger
{json.dumps(ledger.to_public_dict(), indent=1)}

## Device inventory (from snapshot_info)
{ledger.device_summary[:6000]}
"""
    return base + addendum


def _synthesizer_system() -> str:
    return (
        _load_prompt("synthesizer_system_prompt.md")
        + "\n\n# Vendor reference (for [vendor-doc] tagged steps)\n\n"
        + _load_prompt("vendor_reference.md")
        + "\n\n## Runtime caveats\n"
          "1. bgp_session_status results ARE verified facts about session "
          "establishment (as modeled from configs) — cite them as [verified] "
          "and call the check 'BGP session check' in your output. LIVE session "
          "state at deployment time still belongs in RESIDUAL-UNKNOWNS. "
          "differential_reachability results are the authoritative "
          "before/after comparison of what a change broke; detect_loops backs "
          "any loop/no-loop claim. check_routing remains process-presence "
          "only.\n"
          "2. CONFLICT RULE (binding): failure_impact diffs the FULL flow matrix — "
          "NO_IMPACT means no flow that worked before stopped working. If "
          "failure_impact says NO_IMPACT but a reachability probe from a SINGLE "
          "source says NO_ROUTE, the evidence conflicts and is incomplete: the "
          "single source is usually the modified device itself, whose local view "
          "is the outlier. In that case you MUST NOT issue NO-GO (or GO) from the "
          "single-source probe — the verdict floor is INSUFFICIENT-DATA, naming "
          "the missing probes (same destination from other source devices). Only "
          "multi-source agreement upgrades the verdict."
    )


@dataclass
class ScenarioResult:
    findings: str
    verdict: str
    tool_log: list[dict]
    clarification: str | None = None   # set when the translator asked a question


def run_scenario_turn(ops: BatfishOps, ledger: Ledger, user_text: str,
                      chat_history: list[dict] | None = None,
                      progress=None) -> ScenarioResult:
    """Full scenario turn. `progress` is an optional callable(str) for UI status."""
    client = _llm()
    notify = progress or (lambda _msg: None)

    # ---- translator loop -------------------------------------------------
    messages = (
        [{"role": "system", "content": _translator_system(ledger)}]
        + list(chat_history or [])
        + [{"role": "user", "content": user_text}]
    )
    tool_log: list[dict] = []
    final_text = ""

    for _ in range(MAX_TRANSLATOR_ITERATIONS):
        response = client.chat.completions.create(
            model=TRANSLATOR_MODEL,
            max_tokens=16000,
            tools=_openai_tools(),
            messages=messages,
        )
        msg = response.choices[0].message
        tool_calls = msg.tool_calls or []
        if not tool_calls:
            final_text = msg.content or ""
            break  # translator finished (READY FOR SYNTHESIS) or asked a question

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [tc.model_dump() for tc in tool_calls],
        })
        for tc in tool_calls:
            name = tc.function.name
            notify(f"Running: {FRIENDLY_CHECK.get(name, name.replace('_', ' '))}")
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as e:
                args, out, is_error = {}, f"ERROR: unparsable tool arguments: {e}", True
            else:
                try:
                    out = _execute_translator_tool(ops, ledger, name, args)
                    is_error = out.startswith("ERROR")
                except MCPToolError as e:
                    out, is_error = f"ERROR: {e}", True
            tool_log.append({"tool": name, "input": args,
                             "result": out, "is_error": is_error})
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": out})

    # Ambiguity / insufficient-data path: no tools were run at all.
    if not tool_log and "READY FOR SYNTHESIS" not in final_text:
        return ScenarioResult("", "", tool_log, clarification=final_text or
                              "I couldn't classify that scenario — can you rephrase?")

    # ---- synthesizer -----------------------------------------------------
    notify("Synthesizing verdict")
    engine_facts = json.dumps(tool_log, indent=1, default=str)
    if len(engine_facts) > 150_000:
        engine_facts = engine_facts[:150_000] + "\n... [tool log truncated]"

    synth_user = f"""SCENARIO (user's words): {user_text}

LEDGER: {json.dumps(ledger.to_public_dict())}

TRANSLATOR NOTE: {final_text or '(none)'}

STRUCTURED TOOL RESULTS (the ONLY source of engine facts):
{engine_facts}
"""
    synth = client.chat.completions.create(
        model=SYNTHESIZER_MODEL,
        max_tokens=16000,
        messages=[
            {"role": "system", "content": _synthesizer_system()},
            {"role": "user", "content": synth_user},
        ],
    )
    answer = synth.choices[0].message.content or ""

    # Split the two zones on the VERDICT line (docs/05)
    idx = answer.find("VERDICT:")
    if idx > 0:
        findings, verdict = answer[:idx].strip(), answer[idx:].strip()
    else:
        findings, verdict = "", answer.strip()
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
