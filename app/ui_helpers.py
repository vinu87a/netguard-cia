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
    # change-mode (Go/No-Go) fields + query-mode (Answer) fields; the parser
    # picks up whichever the synthesizer emitted for this scenario.
    "VERDICT", "ANSWER", "STATUS", "CONFIDENCE",
    "IMPACTED SERVICES / COMPONENTS", "PACKET-FLOW", "EVIDENCE",
    "REASONING", "CONDITIONS", "ROLLBACK", "RESIDUAL-UNKNOWNS",
]
# Tolerant matchers: capable models decorate the output (## headers, **bold**,
# the field value on the next line) — accept all of it rather than fighting for
# byte-exact headers. Leading markdown/space, field name, OPTIONAL colon.
_MD_PREFIX = r"[#>*_\s-]*"
_FIELD_RE = re.compile(
    r"^" + _MD_PREFIX + r"(" + "|".join(re.escape(f) for f in _VERDICT_FIELDS)
    + r")\s*:?\s*(.*)$",
    re.IGNORECASE,
)
# the second zone starts at VERDICT: (change) or ANSWER: (query)
_VERDICT_HEADER_RE = re.compile(r"^" + _MD_PREFIX + r"(?:VERDICT|ANSWER)\b\s*:?",
                                re.IGNORECASE | re.MULTILINE)


def _clean(text: str) -> str:
    text = re.sub(r"\*\*|__|`", "", text)          # strip md emphasis
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.M)
    return text.strip().strip("—-– ").strip()


def split_findings_verdict(answer: str) -> tuple[str, str]:
    """Split the two-zone answer into (findings, verdict). Robust to markdown
    headers and to the verdict header having no colon (e.g. '## VERDICT')."""
    m = _VERDICT_HEADER_RE.search(answer)
    if not m:
        return "", answer.strip()
    findings = answer[:m.start()].strip()
    verdict = answer[m.start():].strip()
    # drop a leading 'FINDINGS' header from the findings zone for cleanliness
    findings = re.sub(r"^" + _MD_PREFIX + r"FINDINGS\b\s*:?\s*", "", findings,
                      flags=re.IGNORECASE).strip()
    return findings, verdict


def strip_internal_terms(text: str) -> str:
    """Safety net for plain language: replace internal identifiers the
    synthesizer should not emit with friendly names. The sub-agent prompt is
    the primary control; this catches leaks."""
    if not text:
        return text
    for internal, friendly in FRIENDLY_CHECK.items():
        text = re.sub(rf"\b{re.escape(internal)}\b", friendly, text)
    text = re.sub(r"\bBatfish\b", "the analyzer", text)
    return text


def parse_verdict(verdict_text: str) -> dict[str, str]:
    """Split the synthesizer's VERDICT zone into {field: text}. Tolerant of
    markdown decoration and of a header line whose value sits on the following
    line(s); multi-line values run until the next field marker."""
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
        ("ANSWER", "Answer"),
        ("STATUS", "Status"),
        ("CONFIDENCE", "Confidence"),
        ("IMPACTED SERVICES / COMPONENTS", "Impacted services"),
        ("PACKET-FLOW", "Packet flow"),
        ("EVIDENCE", "Evidence"),
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
    # query-mode (read-only answer) statuses — not deployment decisions
    "ATTENTION": ("Attention", "⚠️", "#fab219", "#7a5200"),
    "OK": ("OK", "✅", "#0ca30c", "#0a6b0a"),
    "ANSWER": ("Answer", "💬", "#4b7bec", "#274796"),
}


def verdict_status(fields: dict[str, str]) -> tuple[str, str, str, str]:
    """(label, icon, accent_hex, text_hex) for the result banner. Handles both a
    change VERDICT (Go/No-Go) and a query ANSWER (OK / Attention / neutral)."""
    v = (fields.get("VERDICT") or "").upper().replace(" ", "").replace("_", "-")
    if v:  # change mode
        for key in ("GO-WITH-CONDITIONS", "INSUFFICIENT-DATA", "NO-GO", "GO"):
            if key.replace("-", "") in v.replace("-", ""):
                if key == "GO" and "NO-GO" in v.replace("NOGO", "NO-GO"):
                    continue
                return _STATUS[key]
        return _STATUS["INSUFFICIENT-DATA"]
    # query mode: derive from the STATUS field if present, else neutral
    s = (fields.get("STATUS") or "").upper()
    if "ATTENTION" in s or "WARN" in s or "FAIL" in s:
        return _STATUS["ATTENTION"]
    if "OK" in s or "PASS" in s or "HEALTHY" in s:
        return _STATUS["OK"]
    return _STATUS["ANSWER"]


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
    "snapshot_gates": "Health gates",
    "differential_query": "Before/after diff",
    "test_route_policy": "Route-policy test",
    "search_route_policy": "Route-policy search",
    "test_filter": "ACL flow test",
    "search_filter": "ACL flow search",
    "compare_filters": "ACL before/after",
    "filter_line_reachability": "ACL dead-line check",
    "bgp_compatibility": "BGP compatibility check",
    "bgp_rib": "BGP route table",
    "bgp_edges": "BGP adjacencies",
    "prefix_tracer": "Prefix propagation trace",
    "ospf_compatibility": "OSPF compatibility check",
    "ospf_edges": "OSPF adjacencies",
    "ospf_process_config": "OSPF process check",
    "multipath_consistency": "ECMP consistency check",
    "reachability_search": "Reachability proof",
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

        # snapshot_gates carries is_error=True to mean "a gate regressed", not
        # "the tool failed" — let it render via its own case below.
        if e.get("is_error") and tool != "snapshot_gates":
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
        elif tool == "snapshot_gates" and isinstance(r, dict):
            target = "engine health assertions"
            regressed = r.get("regressed_gates") or []
            outcome = (f"{r.get('gates_passed')}/{r.get('gates_run')} passed"
                       + (f" | REGRESSED: {', '.join(regressed)}" if regressed
                          else " | no regressions"))
        elif tool == "differential_query" and isinstance(r, dict):
            cmp_ = r.get("compared", {})
            target = f"{r.get('question')}: {cmp_.get('before')} vs {cmp_.get('after')}"
            outcome = f"{r.get('changed_row_count', '?')} rows changed"
        elif tool == "test_route_policy" and isinstance(r, dict):
            ir = args.get("input_route", {})
            target = f"{ir.get('network', ir.get('prefix', '?'))} {args.get('direction', '')}"
            actions = sorted({str(x.get("Action")) for x in (r.get("results") or [])})
            outcome = (f"{r.get('result_count', 0)} policy results"
                       + (f" | {', '.join(actions)}" if actions else ""))
        elif tool == "search_route_policy" and isinstance(r, dict):
            target = f"action={args.get('action')}"
            n = r.get("counterexample_count", "?")
            outcome = (f"{n} counterexamples"
                       + (" (intent holds)" if n == 0 else ""))
        elif tool == "test_filter" and isinstance(r, dict):
            h = args.get("headers", {})
            target = f"{h.get('dstIps', 'any')}:{h.get('dstPorts', 'any')}/{h.get('ipProtocols', 'any')}"
            actions = sorted({str(x.get("Action")) for x in (r.get("results") or [])})
            outcome = (f"{r.get('result_count', 0)} filter results"
                       + (f" | {', '.join(actions)}" if actions else ""))
        elif tool == "search_filter" and isinstance(r, dict):
            target = (f"action={args.get('action')}"
                      + (" outside-space" if args.get("invert_search") else ""))
            n = r.get("match_count", "?")
            outcome = f"{n} matching flows" + (" (proof holds)" if n == 0 else "")
        elif tool == "compare_filters" and isinstance(r, dict):
            cmp_ = r.get("compared", {})
            target = f"{cmp_.get('before')} vs {cmp_.get('after')}"
            outcome = f"{r.get('changed_line_count', '?')} filter lines changed"
        elif tool == "filter_line_reachability" and isinstance(r, dict):
            target = "shadowed/dead ACL lines"
            outcome = f"{r.get('unreachable_line_count', '?')} unreachable lines"
        elif tool == "bgp_compatibility" and isinstance(r, dict):
            target = "configured BGP sessions"
            outcome = (", ".join(f"{k} {v}" for k, v in (r.get("summary") or {}).items())
                       or f"{r.get('problem_count', '?')} problems")
        elif tool == "bgp_rib" and isinstance(r, dict):
            target = f"BGP RIB{(' for ' + args['prefix']) if args.get('prefix') else ''}"
            outcome = f"{r.get('route_count', '?')} BGP routes"
        elif tool == "bgp_edges" and isinstance(r, dict):
            target = "BGP adjacencies"
            outcome = f"{r.get('edge_count', '?')} peerings"
        elif tool == "prefix_tracer" and isinstance(r, dict):
            target = f"propagation of {r.get('prefix')}"
            outcome = f"{r.get('row_count', '?')} propagation records"
        elif tool == "ospf_compatibility" and isinstance(r, dict):
            target = "OSPF neighbor pairs"
            outcome = (", ".join(f"{k} {v}" for k, v in (r.get("summary") or {}).items())
                       or f"{r.get('problem_count', '?')} problems")
        elif tool == "ospf_edges" and isinstance(r, dict):
            target = "OSPF adjacencies"
            outcome = f"{r.get('edge_count', '?')} adjacencies"
        elif tool == "ospf_process_config" and isinstance(r, dict):
            target = "OSPF processes"
            outcome = f"{r.get('process_count', '?')} processes"
        elif tool == "routes_to" and isinstance(r, dict):
            target = f"RIB routes to {args.get('prefix')}"
            outcome = f"{r.get('route_count')} routes"
        elif tool == "health_checks" and isinstance(r, dict):
            target = "config health bundle"
            outcome = (f"init issues {r.get('init_issues', {}).get('count', '?')}, "
                       f"undef refs {r.get('undefined_references', {}).get('count', '?')}, "
                       f"dup IPs {r.get('duplicate_interface_ips', {}).get('count', '?')}, "
                       f"bad BGP {len(r.get('bgp_sessions', {}).get('not_established', []))}, "
                       f"loops {r.get('forwarding_loops', {}).get('loop_count', '?')}, "
                       f"unused {r.get('unused_structures', {}).get('count', '?')}, "
                       f"parse warns {r.get('parse_warnings', {}).get('count', '?')}")
        elif tool == "multipath_consistency" and isinstance(r, dict):
            target = "ECMP path consistency"
            n = r.get("inconsistent_count", "?")
            outcome = f"{n} inconsistent flows" + (" (clean)" if n == 0 else "")
        elif tool == "reachability_search" and isinstance(r, dict):
            target = (f"{args.get('start_location', 'any')} -> "
                      f"{args.get('end_location', 'any')} [{args.get('actions')}]")
            n = r.get("example_count", "?")
            outcome = (f"{n} example flows"
                       + (" (intent proven — none found)" if n == 0 else
                          " (counterexamples exist)"))
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


# ---------------------------------------------------------------------------
# Icon-based topology figures (networkx layout + matplotlib + device PNGs)
# ---------------------------------------------------------------------------
from functools import lru_cache
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"


KNOWN_KINDS = ("router", "switch", "firewall", "loadbalancer", "server",
               "wireless", "internet", "isp", "vpn", "device")

# hostname-hint patterns, checked in order (first match wins)
_NAME_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("firewall",     ("firewall", "-fw", "fw-", "asa", "srx", "palo", "forti",
                       "checkpoint")),
    ("loadbalancer", ("-lb", "lb-", "loadbal", "balancer", "f5-", "netscaler",
                       "haproxy")),
    ("vpn",          ("vpn", "ipsec", "tunnel-gw")),
    ("wireless",     ("-ap", "ap-", "wlc", "wifi", "wireless", "wlan")),
    ("server",       ("host", "server", "-srv", "srv-", "www", "-db", "db-",
                       "app-")),
    ("internet",     ("internet",)),
    ("isp",          ("isp",)),
    ("switch",       ("switch", "-sw", "sw-", "tor-", "-tor", "access-")),
    ("router",       ("rtr", "router", "border", "core", "spine", "leaf",
                       "edge", "-gw", "gw-", "pe-", "-pe", "ce-", "-ce",
                       "dist", "agg")),
]


def _kind_from_name(name: str) -> str | None:
    n = name.lower()
    for kind, hints in _NAME_HINTS:
        if any(h in n for h in hints):
            return kind
    return None


@lru_cache(maxsize=16)
def _icon(kind: str, variant: str):
    """Load an icon PNG; unknown kinds or missing files fall back to the
    neutral 'device' icon so a new device type can never crash the diagram."""
    import matplotlib.pyplot as plt
    try:
        return plt.imread(str(ASSETS / f"{kind}_{variant}.png"))
    except FileNotFoundError:
        return plt.imread(str(ASSETS / f"device_{variant}.png"))


def classify_devices(configs: dict[str, str]) -> dict[str, str]:
    """{hostname(lowercase): kind} sniffed from raw config text —
    deterministic, no engine call. Config markers outrank name hints; keys
    are lowercased because the analysis engine lowercases node names."""
    types: dict[str, str] = {}
    for text in configs.values():
        # Cisco `hostname X` or Juniper `host-name X;`
        m = (re.search(r"^\s*hostname\s+(\S+)", text, re.M)
             or re.search(r"host-name\s+(\S+?);", text))
        if not m:
            continue
        name = m.group(1).lower()

        # 1) config-content markers (strongest evidence)
        kind = None
        if ("ASA Version" in text                                  # Cisco ASA
                or re.search(r"^\s*access-list \S+ extended ", text, re.M)
                or re.search(r"^\s*(security \{|set security zones)", text, re.M)  # Juniper SRX
                or re.search(r"^\s*zone-pair security", text, re.M)):  # IOS ZBF
            kind = "firewall"
        elif re.search(r"^\s*crypto (map|ikev2|isakmp)", text, re.M):
            kind = "vpn"
        elif (re.search(r"^\s*switchport", text, re.M)
                or re.search(r"^\s*spanning-tree mode", text, re.M)):
            kind = "switch"

        # 2) hostname conventions, 3) default: parsed configs are routers
        types[name] = kind or _kind_from_name(name) or "router"
    return types


def device_icon_kind(node: str, device_types: dict[str, str] | None = None) -> str:
    """Icon for a node: the classified map first, then hostname hints. A node
    we know nothing about gets the neutral 'device' icon — it should look
    unknown, not masquerade as a router."""
    n = node.lower()
    kind = (device_types or {}).get(n)
    if kind in KNOWN_KINDS:
        return kind
    # engine-generated nodes (ISP modeling) and anything not uploaded
    return _kind_from_name(n) or "device"


def topology_figure(edges: list[dict], removed_edges: set[frozenset] | None = None,
                    failed_nodes: list[str] | None = None,
                    device_types: dict[str, str] | None = None,
                    title: str = ""):
    """Topology as a matplotlib Figure with device icons. Lost links render
    dashed red; failed devices get the red icon variant + FAILED label."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import networkx as nx
    from matplotlib.offsetbox import AnnotationBbox, OffsetImage

    pairs = _node_pairs(edges)
    removed = removed_edges or set()
    failed = set(failed_nodes or [])
    G = nx.Graph()
    for p in pairs | removed:
        a, b = sorted(p)
        G.add_edge(a, b)
    for n in failed:
        G.add_node(n)
    if not G.nodes:
        G.add_node("(no devices)")

    n = len(G.nodes)
    pos = nx.spring_layout(G, seed=7, k=1.6 / max(n, 2) ** 0.5, iterations=200)

    fig, ax = plt.subplots(figsize=(6.4, 4.4), dpi=150)
    ax.set_axis_off()
    if title:
        ax.set_title(title, fontsize=11, fontweight="bold", color="#1F4E79",
                     pad=10)

    for p in pairs:
        a, b = tuple(p)
        ax.plot(*zip(pos[a], pos[b]), color="#8CA3BA", lw=1.4, zorder=1)
    for p in removed:
        a, b = tuple(p)
        ax.plot(*zip(pos[a], pos[b]), color="#C0392B", lw=1.8, ls=(0, (5, 4)),
                zorder=1)

    zoom = 0.11 if n <= 8 else (0.085 if n <= 14 else 0.065)
    for node, (x, y) in pos.items():
        kind = device_icon_kind(node, device_types)
        variant = "red" if node in failed else "steel"
        ab = AnnotationBbox(OffsetImage(_icon(kind, variant), zoom=zoom),
                            (x, y), frameon=False, zorder=3)
        ax.add_artist(ab)
        label = node + ("  ✕ FAILED" if node in failed else "")
        ax.annotate(label, (x, y), xytext=(0, -16 if n <= 14 else -13),
                    textcoords="offset points", ha="center", fontsize=6.5,
                    color="#C0392B" if node in failed else "#12212F",
                    fontweight="bold" if node in failed else "normal", zorder=4)

    ax.margins(0.12)
    fig.tight_layout()
    return fig


def before_after_figures(base_edges: list[dict], current_edges: list[dict],
                         failed_nodes: list[str] | None = None,
                         device_types: dict[str, str] | None = None):
    """(before_fig, after_fig) with device icons; the after figure shows lost
    links dashed red and failed devices red."""
    removed = _node_pairs(base_edges) - _node_pairs(current_edges)
    before = topology_figure(base_edges, device_types=device_types,
                             title="BEFORE — original network")
    after = topology_figure(current_edges, removed_edges=removed,
                            failed_nodes=failed_nodes,
                            device_types=device_types,
                            title="AFTER — with the change applied")
    return before, after
