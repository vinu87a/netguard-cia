"""
Presentation helpers for the Streamlit UI: verdict parsing, engine-facts
tables, and topology DOT diagrams. Pure functions — no Streamlit imports —
so they are unit-testable headlessly.
"""
from __future__ import annotations

import json
import re

# ---------------------------------------------------------------------------
# Verdict parsing (docs/05 two-zone format -> summary table rows)
# ---------------------------------------------------------------------------
_VERDICT_FIELDS = [
    "VERDICT", "CONFIDENCE", "IMPACTED SERVICES / COMPONENTS", "PACKET-FLOW",
    "REASONING", "CONDITIONS", "ROLLBACK", "RESIDUAL-UNKNOWNS",
]
_FIELD_RE = re.compile(
    r"^\W*(" + "|".join(re.escape(f) for f in _VERDICT_FIELDS) + r")\W*:\s*(.*)$",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    text = re.sub(r"\*\*|__|`", "", text)          # strip md emphasis
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.M)
    return text.strip().strip("—-– ").strip()


def parse_verdict(verdict_text: str) -> dict[str, str]:
    """Split the synthesizer's VERDICT zone into {field: text}. Tolerant of
    markdown decoration; multi-line values run until the next field marker."""
    fields: dict[str, str] = {}
    current: str | None = None
    # models emit unicode hyphens (non-breaking U+2011, en/em dashes) inside
    # field names like PACKET-FLOW — normalize before matching
    dash_map = str.maketrans({"‑": "-", "–": "-", "—": "-"})
    for line in verdict_text.splitlines():
        m = _FIELD_RE.match(line.translate(dash_map))
        if m:
            current = m.group(1).upper()
            fields[current] = m.group(2).strip()
        elif current:
            fields[current] += "\n" + line
    return {k: _clean(v) for k, v in fields.items() if _clean(v)}


def verdict_summary_rows(fields: dict[str, str]) -> list[tuple[str, str]]:
    """The one-glance rows for the summary table (full prose stays below)."""
    order = [
        ("VERDICT", "Verdict"),
        ("CONFIDENCE", "Confidence"),
        ("IMPACTED SERVICES / COMPONENTS", "Impacted services"),
        ("PACKET-FLOW", "Packet flow"),
        ("CONDITIONS", "Conditions"),
        ("ROLLBACK", "Rollback"),
    ]
    rows = []
    for key, label in order:
        val = fields.get(key)
        if val:
            first = " / ".join(x.strip() for x in val.splitlines() if x.strip())
            rows.append((label, first[:300]))
    return rows


# ---------------------------------------------------------------------------
# Verdict status (banner color/icon) — status colors are reserved and always
# paired with icon + label, never color alone
# ---------------------------------------------------------------------------
_STATUS = {
    "GO-WITH-CONDITIONS": ("GO — with conditions", "⚠️", "#fab219", "#7a5200"),
    "INSUFFICIENT-DATA": ("Insufficient data", "❔", "#8a94a1", "#3d4652"),
    "NO-GO": ("NO-GO", "⛔", "#d03b3b", "#8f1f1f"),
    "GO": ("GO", "✅", "#0ca30c", "#0a6b0a"),
}


def verdict_status(fields: dict[str, str]) -> tuple[str, str, str, str]:
    """(label, icon, accent_hex, text_hex) for the verdict banner."""
    v = (fields.get("VERDICT") or "").upper().replace(" ", "").replace("_", "-")
    for key in ("GO-WITH-CONDITIONS", "INSUFFICIENT-DATA", "NO-GO", "GO"):
        if key.replace("-", "") in v.replace("-", ""):
            if key == "GO" and "NO-GO" in v.replace("NOGO", "NO-GO"):
                continue
            return _STATUS[key]
    return _STATUS["INSUFFICIENT-DATA"]


# ---------------------------------------------------------------------------
# Network-checks table (deterministic, straight from the tool log).
# Check names are PLAIN LANGUAGE — internal tool identifiers stay internal.
# ---------------------------------------------------------------------------
FRIENDLY_CHECK = {
    "apply_failure_set": "Failure simulation",
    "batfish_failure_impact": "Blast-radius check",
    "network_traceroute": "Path trace",
    "batfish_simulate_traffic": "Traffic simulation",
    "network_bidirectional_reachability": "Two-way reachability",
    "bgp_session_status": "BGP session check",
    "differential_reachability": "Before/after comparison",
    "detect_loops": "Loop check",
    "health_checks": "Configuration health check",
    "routes_to": "Route lookup",
    "batfish_check_routing": "Routing process check",
    "stage_change_snapshot": "Configuration change",
}
def _loads(result: str):
    try:
        return json.loads(result)
    except (TypeError, ValueError):
        return None


def facts_rows(tool_log: list[dict]) -> list[dict]:
    """One row per engine call: what was checked, where, and the outcome."""
    rows = []
    for e in tool_log:
        tool, args = e["tool"], e.get("input", {})
        snap = args.get("snapshot") or "current"
        r = _loads(e["result"])
        target, outcome = "", ""

        if e.get("is_error"):
            target = json.dumps(args)[:60]
            outcome = str(e["result"])[:120]
        elif tool == "network_traceroute" and isinstance(r, dict):
            target = f"{args.get('source_location')} -> {args.get('dest_ip')}"
            path = " > ".join(r.get("path_summary") or []) or "-"
            outcome = f"accepted {r.get('accepted', 0)}/{r.get('trace_count', 0)} | {path}"
        elif tool == "batfish_simulate_traffic" and isinstance(r, dict):
            target = f"{args.get('src')} -> {args.get('dst')}"
            outcome = str(r.get("overall", r))[:120]
        elif tool == "network_bidirectional_reachability" and isinstance(r, dict):
            target = f"{args.get('location_a')} <-> {args.get('location_b')}"
            outcome = (f"forward {r.get('forward_allowed', '?')}, "
                       f"reverse {r.get('reverse_allowed', '?')}")
        elif tool == "bgp_session_status" and isinstance(r, dict):
            target = "all BGP neighbors"
            outcome = ", ".join(f"{k} {v}" for k, v in (r.get("summary") or {}).items())
        elif tool == "differential_reachability" and isinstance(r, dict):
            cmp_ = r.get("compared", {})
            target = f"{cmp_.get('before')} vs {cmp_.get('after')}"
            outcome = f"{r.get('changed_flow_count', '?')} flows changed disposition"
        elif tool == "batfish_failure_impact" and isinstance(r, dict):
            target = f"{args.get('failure_type')} {args.get('target')}"
            outcome = f"{r.get('overall')} ({len(r.get('results') or [])} impacted flows)"
        elif tool == "apply_failure_set" and isinstance(r, dict):
            fs = r.get("active_failure_set", {})
            target = "cumulative failure set"
            outcome = (f"-> {r.get('snapshot')} | nodes {fs.get('nodes')} "
                       f"ifaces {fs.get('interfaces')}")
        elif tool == "stage_change_snapshot" and isinstance(r, dict):
            target = "config edit"
            outcome = f"-> {r.get('snapshot')} (parse {r.get('parse', '?')})"
        elif tool == "detect_loops" and isinstance(r, dict):
            target = "forwarding loops"
            outcome = f"{r.get('loop_count')} loops"
        elif tool == "routes_to" and isinstance(r, dict):
            target = f"RIB routes to {args.get('prefix')}"
            outcome = f"{r.get('route_count')} routes"
        elif tool == "health_checks" and isinstance(r, dict):
            target = "config health bundle"
            outcome = (f"init issues {r.get('init_issues', {}).get('count', '?')}, "
                       f"undef refs {r.get('undefined_references', {}).get('count', '?')}, "
                       f"dup IPs {r.get('duplicate_interface_ips', {}).get('count', '?')}, "
                       f"bad BGP {len(r.get('bgp_sessions', {}).get('not_established', []))}, "
                       f"loops {r.get('forwarding_loops', {}).get('loop_count', '?')}")
        elif tool == "batfish_check_routing" and isinstance(r, dict):
            target = f"protocols {args.get('protocols')}"
            outcome = f"process check {r.get('overall', '?')}"
        elif tool in ("read_config", "get_snapshot_info"):
            continue  # bookkeeping, not a network fact
        else:
            target = json.dumps(args)[:60]
            outcome = str(e["result"])[:100]

        rows.append({"Check": FRIENDLY_CHECK.get(tool, tool.replace("_", " ")),
                     "Target": target, "Network state": snap,
                     "Result": outcome})
    return rows


# ---------------------------------------------------------------------------
# Topology diagrams (Graphviz DOT from layer-3 edges)
# ---------------------------------------------------------------------------
def _node_pairs(edges: list[dict]) -> set[frozenset]:
    return {frozenset((e["n1"], e["n2"])) for e in edges if e["n1"] != e["n2"]}


def topology_dot(edges: list[dict], removed_edges: set[frozenset] | None = None,
                 failed_nodes: list[str] | None = None,
                 title: str = "", max_size: str = "7,4.5") -> str:
    """Node-level topology DOT. removed_edges render dashed red; failed_nodes
    fill red. max_size ("W,H" inches) caps the rendered diagram size."""
    pairs = _node_pairs(edges)
    removed = removed_edges or set()
    failed = set(failed_nodes or [])
    nodes = {n for p in pairs | removed for n in p} | failed

    lines = [
        "graph topology {",
        '  layout=neato; overlap=false; splines=true;',
        f'  size="{max_size}"; ratio=compress;',
        f'  label="{title}"; labelloc=t; fontsize=14; fontname="Helvetica";',
        '  node [shape=box, style="rounded,filled", fillcolor="#eef4fb",'
        ' fontname="Helvetica", fontsize=11];',
        '  edge [color="#7a8aa0"];',
    ]
    for n in sorted(nodes):
        style = ' , fillcolor="#f8d3d3", color="#b03030"' if n in failed else ""
        lines.append(f'  "{n}" [label="{n}"{style.replace(" ,", ",")}];')
    for p in sorted(pairs, key=sorted):
        a, b = sorted(p)
        lines.append(f'  "{a}" -- "{b}";')
    for p in sorted(removed, key=sorted):
        a, b = sorted(p)
        lines.append(f'  "{a}" -- "{b}" [style=dashed, color="#c0392b", penwidth=2];')
    lines.append("}")
    return "\n".join(lines)


def before_after_dots(base_edges: list[dict], current_edges: list[dict],
                      failed_nodes: list[str] | None = None) -> tuple[str, str]:
    """(before_dot, after_dot); the after diagram shows edges lost since base
    as dashed red and failed nodes filled red."""
    base_pairs = _node_pairs(base_edges)
    cur_pairs = _node_pairs(current_edges)
    removed = base_pairs - cur_pairs
    before = topology_dot(base_edges, title="BEFORE (base)")
    after = topology_dot(current_edges, removed_edges=removed,
                         failed_nodes=failed_nodes, title="AFTER (current)")
    return before, after
