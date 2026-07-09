"""
Direct pybatfish access to the Batfish engine (localhost:9996 by default).

Why this exists alongside mcp_client.py: the MCP container exposes ~12 useful
tools but hides most of Batfish's question set. Studying batfish/pybatfish
showed four capabilities that materially improve verdict quality:

  1. fork_snapshot(deactivate_nodes=[...], deactivate_interfaces=[...]) takes
     LISTS -> stacked failures are ONE cheap fork, no config re-editing
     (docs/04 "Case A", previously thought unavailable).
  2. bgpSessionStatus -> real session establishment facts (previously a
     mandatory RESIDUAL-UNKNOWN in every verdict).
  3. differentialReachability -> native engine diff of what a change broke
     between two snapshots (previously hand-compared traceroutes).
  4. detectLoops / initIssues / undefinedReferences / duplicate-IP checks ->
     engine-backed health findings (previously not checkable).

Same hard rule applies: this module only RUNS engine questions and returns
structured results. No reasoning here.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pybatfish.client.session import Session
from pybatfish.client.asserts import (
    assert_no_duplicate_router_ids,
    assert_no_forwarding_loops,
    assert_no_incompatible_bgp_sessions,
    assert_no_incompatible_ospf_sessions,
    assert_no_undefined_references,
    assert_no_unestablished_bgp_sessions,
)
from pybatfish.datamodel.flow import HeaderConstraints
from pybatfish.datamodel.primitives import Interface
from pybatfish.datamodel.route import BgpRoute, BgpRouteConstraints
from pybatfish.exception import BatfishAssertException

BATFISH_DIRECT_HOST = os.environ.get("BATFISH_DIRECT_HOST", "localhost")
# No row cap — the worker has a large context window; return all rows.
MAX_ROWS = None


def _records(df, cap: int | None = MAX_ROWS) -> list[dict]:
    """DataFrame -> JSON-safe records (pybatfish cells hold rich objects)."""
    recs = (df if cap is None else df.head(cap)).to_dict(orient="records")
    return json.loads(json.dumps(recs, default=str))


class DirectEngine:
    def __init__(self, host: str = BATFISH_DIRECT_HOST):
        self.host = host

    def _session(self, network: str, snapshot: str | None = None) -> Session:
        bf = Session(host=self.host)
        bf.set_network(network)
        if snapshot:
            bf.set_snapshot(snapshot)
        return bf

    # -- snapshot init (used by the app's upload turn for fork-capable base) --
    def init_snapshot(self, network: str, snapshot: str,
                      configs: dict[str, str]) -> None:
        bf = self._session(network)
        with tempfile.TemporaryDirectory() as td:
            cfg_dir = Path(td) / "configs"
            cfg_dir.mkdir()
            for fn, text in configs.items():
                (cfg_dir / fn).write_text(text)
            bf.init_snapshot(td, name=snapshot, overwrite=True)

    # -- stacked failures: one fork, whole failure set (docs/04 Case A) -------
    def fork_with_failures(self, network: str, base_snapshot: str, name: str,
                           node_failures: list[str],
                           interface_failures: list[str]) -> str:
        """interface_failures use the node[iface] format, e.g.
        as1border1[GigabitEthernet1/0]."""
        bf = self._session(network)
        ifaces = []
        for spec in interface_failures:
            if "[" not in spec or not spec.endswith("]"):
                raise ValueError(f"bad interface spec {spec!r}, want node[iface]")
            node, iface = spec[:-1].split("[", 1)
            ifaces.append(Interface(hostname=node, interface=iface))
        result = bf.fork_snapshot(
            base_name=base_snapshot,
            name=name,
            overwrite=True,
            deactivate_nodes=node_failures or None,
            deactivate_interfaces=ifaces or None,
        )
        if result is None:
            raise RuntimeError("fork_snapshot failed")
        return result

    # -- session facts ---------------------------------------------------------
    def bgp_sessions(self, network: str, snapshot: str) -> dict:
        bf = self._session(network, snapshot)
        df = bf.q.bgpSessionStatus().answer().frame()
        status_counts = df["Established_Status"].value_counts().to_dict()
        bad = df[df["Established_Status"] != "ESTABLISHED"]
        return {
            "summary": {str(k): int(v) for k, v in status_counts.items()},
            "not_established": _records(
                bad[["Node", "VRF", "Local_IP", "Remote_Node", "Remote_IP",
                     "Established_Status"]]
            ),
        }

    # -- BGP depth: why a peer breaks, learned routes, adjacencies, propagation -
    def bgp_compatibility(self, network: str, snapshot: str,
                          nodes: str | None = None,
                          remote_nodes: str | None = None) -> dict:
        """Configured BGP session compatibility — WHY a session will/won't come
        up (config-level match), complementing bgp_session_status (established
        state). Surfaces NO_MATCH_FOUND / incompatible pairings."""
        bf = self._session(network, snapshot)
        kw: dict[str, Any] = {}
        if nodes:
            kw["nodes"] = nodes
        if remote_nodes:
            kw["remoteNodes"] = remote_nodes
        df = bf.q.bgpSessionCompatibility(**kw).answer().frame()
        status_col = next((c for c in df.columns if "Status" in c), None)
        summary: dict[str, int] = {}
        problems = df
        if status_col:
            summary = {str(k): int(v)
                       for k, v in df[status_col].value_counts().items()}
            problems = df[~df[status_col].astype(str).isin(
                ["UNIQUE_MATCH", "DYNAMIC_MATCH"])]
        return {"summary": summary, "problem_count": int(len(problems)),
                "problems": _records(problems)}

    def bgp_rib(self, network: str, snapshot: str, nodes: str | None = None,
                prefix: str | None = None) -> dict:
        """Routes in the BGP RIB (learned via BGP, before best-path selection)."""
        bf = self._session(network, snapshot)
        kw: dict[str, Any] = {}
        if nodes:
            kw["nodes"] = nodes
        if prefix:
            kw["network"] = prefix
        df = bf.q.bgpRib(**kw).answer().frame()
        return {"route_count": int(len(df)), "routes": _records(df)}

    def bgp_edges(self, network: str, snapshot: str, nodes: str | None = None,
                  remote_nodes: str | None = None) -> dict:
        """Established BGP adjacencies (who peers with whom)."""
        bf = self._session(network, snapshot)
        kw: dict[str, Any] = {}
        if nodes:
            kw["nodes"] = nodes
        if remote_nodes:
            kw["remoteNodes"] = remote_nodes
        df = bf.q.bgpEdges(**kw).answer().frame()
        return {"edge_count": int(len(df)), "edges": _records(df)}

    def prefix_tracer(self, network: str, snapshot: str, prefix: str,
                      nodes: str | None = None) -> dict:
        """Trace how a prefix propagates (originated / received / advertised /
        installed) across the network — 'does this prefix still reach X after
        the change'."""
        bf = self._session(network, snapshot)
        kw: dict[str, Any] = {"prefix": prefix}
        if nodes:
            kw["nodes"] = nodes
        df = bf.q.prefixTracer(**kw).answer().frame()
        return {"prefix": prefix, "row_count": int(len(df)),
                "trace": _records(df)}

    # -- OSPF (the other IGP; 38 lab configs use it) ----------------------------
    def ospf_compatibility(self, network: str, snapshot: str,
                           nodes: str | None = None,
                           remote_nodes: str | None = None) -> dict:
        """OSPF adjacency compatibility — incompatible/unestablished neighbor
        pairs and why (area/network-type/MTU/timer mismatches)."""
        bf = self._session(network, snapshot)
        kw: dict[str, Any] = {}
        if nodes:
            kw["nodes"] = nodes
        if remote_nodes:
            kw["remoteNodes"] = remote_nodes
        df = bf.q.ospfSessionCompatibility(**kw).answer().frame()
        status_col = next((c for c in df.columns if "Status" in c), None)
        summary: dict[str, int] = {}
        problems = df
        if status_col:
            summary = {str(k): int(v)
                       for k, v in df[status_col].value_counts().items()}
            problems = df[df[status_col].astype(str) != "ESTABLISHED"]
        return {"summary": summary, "problem_count": int(len(problems)),
                "problems": _records(problems)}

    def ospf_edges(self, network: str, snapshot: str, nodes: str | None = None,
                   remote_nodes: str | None = None) -> dict:
        """Established OSPF adjacencies."""
        bf = self._session(network, snapshot)
        kw: dict[str, Any] = {}
        if nodes:
            kw["nodes"] = nodes
        if remote_nodes:
            kw["remoteNodes"] = remote_nodes
        df = bf.q.ospfEdges(**kw).answer().frame()
        return {"edge_count": int(len(df)), "edges": _records(df)}

    def ospf_process_config(self, network: str, snapshot: str,
                            nodes: str | None = None) -> dict:
        """OSPF process configuration (router-id, areas, reference bandwidth …)."""
        bf = self._session(network, snapshot)
        kw: dict[str, Any] = {}
        if nodes:
            kw["nodes"] = nodes
        df = bf.q.ospfProcessConfiguration(**kw).answer().frame()
        return {"process_count": int(len(df)), "processes": _records(df)}

    # -- native diff: what did the change break --------------------------------
    def differential_reachability(self, network: str, snapshot: str,
                                  reference_snapshot: str) -> dict:
        """Flows whose disposition changed between reference (before) and
        snapshot (after). SUCCESS->FAILURE rows are what the change broke."""
        import re

        bf = self._session(network)
        ans = bf.q.differentialReachability().answer(
            snapshot=snapshot, reference_snapshot=reference_snapshot
        )
        df = ans.frame()

        dispositions = re.compile(
            r"ACCEPTED|DENIED_IN|DENIED_OUT|NO_ROUTE|NULL_ROUTED|"
            r"NEIGHBOR_UNREACHABLE|LOOP|INSUFFICIENT_INFO|EXITS_NETWORK|"
            r"DELIVERED_TO_SUBNET"
        )

        def dispo(cell) -> str:
            found = sorted(set(dispositions.findall(str(cell))))
            return "/".join(found) or "?"

        return {
            "changed_flow_count": int(len(df)),
            "note": ("flows whose disposition differs between the snapshots; "
                     "empty means the change broke (and fixed) nothing. "
                     "before/after are the trace disposition sets."),
            "changed_flows": [
                {"flow": str(r.get("Flow")),
                 "before": dispo(r.get("Reference_Traces")),
                 "after": dispo(r.get("Snapshot_Traces"))}
                for r in df.to_dict(orient="records")
            ],
        }

    # -- loops ------------------------------------------------------------------
    def detect_loops(self, network: str, snapshot: str) -> dict:
        bf = self._session(network, snapshot)
        df = bf.q.detectLoops().answer().frame()
        return {"loop_count": int(len(df)), "loops": _records(df)}

    # -- deterministic Go/No-Go gates (pybatfish.client.asserts) ----------------
    # The engine's OWN definition of a healthy snapshot, expressed as
    # parameterless assertions. These form the hard verdict FLOOR: run on the
    # change snapshot before any GO, and the LLM verifier may only tighten them,
    # never override. soft=False raises BatfishAssertException carrying the
    # offending rows, which we surface verbatim as the failure detail.
    _GATES = (
        ("no_forwarding_loops", assert_no_forwarding_loops),
        ("no_incompatible_bgp_sessions", assert_no_incompatible_bgp_sessions),
        ("no_unestablished_bgp_sessions", assert_no_unestablished_bgp_sessions),
        ("no_incompatible_ospf_sessions", assert_no_incompatible_ospf_sessions),
        ("no_undefined_references", assert_no_undefined_references),
        ("no_duplicate_router_ids", assert_no_duplicate_router_ids),
    )

    def snapshot_gates(self, network: str, snapshot: str) -> dict:
        """Run the parameterless assertion gates against one snapshot and return
        a structured pass/fail floor. A gate that ERRORS (e.g. a question needs a
        dataplane that could not be computed) is reported as passed=None, not
        failed — an inconclusive gate must not masquerade as a clean pass."""
        bf = self._session(network, snapshot)
        results: list[dict] = []
        for name, fn in self._GATES:
            try:
                fn(session=bf, snapshot=snapshot)
                results.append({"gate": name, "passed": True})
            except BatfishAssertException as e:
                results.append({"gate": name, "passed": False,
                                "detail": str(e)[:2000]})
            except Exception as e:  # noqa: BLE001 — inconclusive, not a pass
                results.append({"gate": name, "passed": None,
                                "error": f"{type(e).__name__}: {str(e)[:300]}"})
        failed = [r for r in results if r["passed"] is False]
        inconclusive = [r for r in results if r["passed"] is None]
        return {
            "gates_run": len(results),
            "gates_passed": sum(1 for r in results if r["passed"] is True),
            "gates_failed": len(failed),
            "gates_inconclusive": len(inconclusive),
            "all_passed": not failed and not inconclusive,
            "results": results,
        }

    # -- generic native diff: any table question, base-vs-change ----------------
    # Batfish runs ANY table question differentially when .answer() gets a
    # reference_snapshot, returning only rows that differ between the two
    # snapshots. This is the engine-authoritative "what changed" that replaces
    # app-side hand-diffing, and it is the FINDINGS engine for edit scenarios.
    DIFFABLE_QUESTIONS = {
        "routes", "bgpRib", "bgpSessionStatus", "bgpPeerConfiguration",
        "bgpEdges", "interfaceProperties", "nodeProperties",
        "definedStructures", "undefinedReferences", "namedStructures",
        "ospfEdges", "edges",
    }

    def diff(self, network: str, before: str, after: str,
             question: str = "routes",
             question_args: dict | None = None) -> dict:
        """Run one table question differentially (reference=before, snapshot=
        after) and return only the changed rows. `question` must be in
        DIFFABLE_QUESTIONS; `question_args` are passed to the question builder
        (e.g. {'nodes': '/as1/'} or {'network': '2.128.0.0/16'})."""
        if question not in self.DIFFABLE_QUESTIONS:
            raise ValueError(
                f"question {question!r} is not diffable; choose one of "
                f"{sorted(self.DIFFABLE_QUESTIONS)}")
        if before == after:
            raise ValueError(f"before and after are both {after!r}")
        bf = self._session(network)
        builder = getattr(bf.q, question)
        q = builder(**(question_args or {}))
        df = q.answer(snapshot=after, reference_snapshot=before).frame()
        return {
            "question": question,
            "compared": {"before": before, "after": after},
            "changed_row_count": int(len(df)),
            "note": ("rows that differ between the two snapshots (present in "
                     "only one, or with changed values); empty means this "
                     "question sees no change from the edit"),
            "changed": _records(df),
        }

    # -- config/health checks ----------------------------------------------------
    def health_checks(self, network: str, snapshot: str) -> dict:
        bf = self._session(network, snapshot)
        out: dict[str, Any] = {}

        df = bf.q.initIssues().answer().frame()
        out["init_issues"] = {"count": int(len(df)), "rows": _records(df)}

        df = bf.q.undefinedReferences().answer().frame()
        out["undefined_references"] = {"count": int(len(df)), "rows": _records(df)}

        # duplicate primary IPs across devices (e.g. cloned loopbacks)
        df = bf.q.interfaceProperties(properties="Primary_Address").answer().frame()
        df = df.dropna(subset=["Primary_Address"])
        dupes = df[df.duplicated(subset=["Primary_Address"], keep=False)]
        out["duplicate_interface_ips"] = {
            "count": int(len(dupes)),
            "rows": _records(dupes.sort_values("Primary_Address")),
        }

        sessions = self.bgp_sessions(network, snapshot)
        out["bgp_sessions"] = sessions

        out["forwarding_loops"] = self.detect_loops(network, snapshot)

        # config hygiene: dead structures + parse warnings
        df = bf.q.unusedStructures().answer().frame()
        out["unused_structures"] = {"count": int(len(df)), "rows": _records(df)}
        df = bf.q.parseWarning().answer().frame()
        out["parse_warnings"] = {"count": int(len(df)), "rows": _records(df)}
        return out

    # -- forwarding integrity: ECMP / multipath consistency ---------------------
    def multipath_consistency(self, network: str, snapshot: str) -> dict:
        """Flows whose equal-cost multipath disagree — some copies delivered,
        others dropped (asymmetric/inconsistent forwarding, a real outage
        cause). Empty is clean."""
        bf = self._session(network, snapshot)
        df = bf.q.subnetMultipathConsistency().answer().frame()
        return {"inconsistent_count": int(len(df)),
                "note": ("flows whose ECMP paths disagree on delivery; empty "
                         "means all multipath is consistent"),
                "inconsistent_flows": _records(df)}

    # -- route policies (route-map / policy-statement analysis) -----------------
    @staticmethod
    def _bgp_route(spec: dict) -> BgpRoute:
        """Build a BgpRoute from a loose dict; only `network` (the prefix) is
        truly required — everything else gets a sane default so the caller can
        specify just the announcement it cares about."""
        if "network" not in spec and "prefix" in spec:
            spec = {**spec, "network": spec["prefix"]}
        return BgpRoute(
            network=spec["network"],
            originatorIp=spec.get("originatorIp", "0.0.0.0"),
            originType=spec.get("originType", "egp"),
            protocol=spec.get("protocol", "bgp"),
            asPath=spec.get("asPath", []),
            communities=spec.get("communities", []),
            localPreference=spec.get("localPreference", 0),
            metric=spec.get("metric", 0),
        )

    def test_route_policy(self, network: str, snapshot: str, input_routes,
                          direction: str, policies: str | None = None,
                          nodes: str | None = None) -> dict:
        """Evaluate how a route-map/policy processes a concrete route: returns
        PERMIT/DENY, the output route (modified attributes), and the matched
        clause trace. `direction` is 'in' or 'out'."""
        bf = self._session(network, snapshot)
        routes = input_routes if isinstance(input_routes, list) else [input_routes]
        kwargs: dict[str, Any] = {
            "inputRoutes": [self._bgp_route(r) for r in routes],
            "direction": direction,
        }
        if policies:
            kwargs["policies"] = policies
        if nodes:
            kwargs["nodes"] = nodes
        df = bf.q.testRoutePolicies(**kwargs).answer().frame()
        return {"tested": len(kwargs["inputRoutes"]), "direction": direction,
                "result_count": int(len(df)), "results": _records(df)}

    @staticmethod
    def _route_constraints(spec: dict) -> BgpRouteConstraints:
        return BgpRouteConstraints(
            prefix=spec.get("prefix"),
            complementPrefix=spec.get("complementPrefix"),
            localPreference=spec.get("localPreference"),
            med=spec.get("med"),
            communities=spec.get("communities"),
            asPath=spec.get("asPath"),
        )

    def search_route_policy(self, network: str, snapshot: str, action: str,
                            input_constraints: dict | None = None,
                            output_constraints: dict | None = None,
                            policies: str | None = None,
                            nodes: str | None = None) -> dict:
        """Search for route announcements a policy treats with `action`
        ('permit'|'deny'). Each returned row is a concrete counterexample: e.g.
        searching 'deny' over your intended-permit space, any row is a route the
        policy wrongly drops. Empty results = the intent holds for the whole
        space (a proof, not a sample)."""
        bf = self._session(network, snapshot)
        kwargs: dict[str, Any] = {"action": action}
        if input_constraints:
            kwargs["inputConstraints"] = self._route_constraints(input_constraints)
        if output_constraints:
            kwargs["outputConstraints"] = self._route_constraints(output_constraints)
        if policies:
            kwargs["policies"] = policies
        if nodes:
            kwargs["nodes"] = nodes
        df = bf.q.searchRoutePolicies(**kwargs).answer().frame()
        return {"action_searched": action,
                "counterexample_count": int(len(df)),
                "note": ("each row is a route announcement for which the policy "
                         "takes the searched action; empty means no such route "
                         "exists in the constrained space (an exhaustive proof)"),
                "counterexamples": _records(df)}

    # -- filters / ACLs (provably-safe change analysis) -------------------------
    @staticmethod
    def _headers(spec: dict) -> HeaderConstraints:
        """Build a HeaderConstraints (flow space) from a loose dict. Common keys:
        srcIps, dstIps, srcPorts, dstPorts, ipProtocols, applications."""
        return HeaderConstraints(
            srcIps=spec.get("srcIps"),
            dstIps=spec.get("dstIps"),
            srcPorts=spec.get("srcPorts"),
            dstPorts=spec.get("dstPorts"),
            ipProtocols=spec.get("ipProtocols"),
            applications=spec.get("applications"),
        )

    def test_filter(self, network: str, snapshot: str, headers: dict,
                    filters: str | None = None, nodes: str | None = None,
                    start_location: str | None = None) -> dict:
        """Deterministically evaluate whether a filter/ACL PERMITs or DENYs a
        specific flow (headers), returning the matched line per filter."""
        bf = self._session(network, snapshot)
        kwargs: dict[str, Any] = {"headers": self._headers(headers)}
        if filters:
            kwargs["filters"] = filters
        if nodes:
            kwargs["nodes"] = nodes
        if start_location:
            kwargs["startLocation"] = start_location
        df = bf.q.testFilters(**kwargs).answer().frame()
        return {"result_count": int(len(df)), "results": _records(df)}

    def search_filter(self, network: str, snapshot: str, headers: dict,
                      action: str, invert_search: bool = False,
                      filters: str | None = None, nodes: str | None = None,
                      start_location: str | None = None) -> dict:
        """Search the flow space for flows a filter treats with `action`
        ('permit'|'deny'). With invert_search=True, search OUTSIDE the given
        header space (the collateral-damage check). Empty results under a
        'deny' search of intended-permit traffic = every intended flow is
        allowed (a proof)."""
        bf = self._session(network, snapshot)
        kwargs: dict[str, Any] = {"headers": self._headers(headers),
                                  "action": action,
                                  "invertSearch": invert_search}
        if filters:
            kwargs["filters"] = filters
        if nodes:
            kwargs["nodes"] = nodes
        if start_location:
            kwargs["startLocation"] = start_location
        df = bf.q.searchFilters(**kwargs).answer().frame()
        return {"action_searched": action, "invert_search": invert_search,
                "match_count": int(len(df)),
                "note": ("flows the filter treats with the searched action "
                         "(invert_search searches OUTSIDE the header space); a "
                         "counterexample-style result, empty = none exist"),
                "matches": _records(df)}

    def compare_filters(self, network: str, before: str, after: str,
                        filters: str | None = None,
                        nodes: str | None = None) -> dict:
        """Filter lines that treat some flow differently between two snapshots —
        the authoritative 'what did this ACL edit change'."""
        bf = self._session(network)
        kwargs: dict[str, Any] = {}
        if filters:
            kwargs["filters"] = filters
        if nodes:
            kwargs["nodes"] = nodes
        df = (bf.q.compareFilters(**kwargs)
              .answer(snapshot=after, reference_snapshot=before).frame())
        return {"compared": {"before": before, "after": after},
                "changed_line_count": int(len(df)),
                "note": ("filter lines whose treatment of some flow differs "
                         "between snapshots; empty = the edit changed no filter "
                         "behavior"),
                "changes": _records(df)}

    def filter_line_reachability(self, network: str, snapshot: str,
                                 filters: str | None = None,
                                 nodes: str | None = None) -> dict:
        """ACL lines that can never match (shadowed/dead lines) — hygiene."""
        bf = self._session(network, snapshot)
        kwargs: dict[str, Any] = {}
        if filters:
            kwargs["filters"] = filters
        if nodes:
            kwargs["nodes"] = nodes
        df = bf.q.filterLineReachability(**kwargs).answer().frame()
        return {"unreachable_line_count": int(len(df)),
                "note": "ACL lines that never match (shadowed/dead); empty is clean",
                "unreachable_lines": _records(df)}

    # -- topology edges (for the UI diagram) ---------------------------------------
    def layer3_edges(self, network: str, snapshot: str) -> list[dict]:
        """Layer-3 adjacencies as [{'n1','i1','n2','i2'}] — feeds the topology
        diagram. Deactivated nodes/interfaces (failure forks) drop out here."""
        bf = self._session(network, snapshot)
        df = bf.q.layer3Edges().answer().frame()

        def side(cell):
            host = getattr(cell, "hostname", None)
            iface = getattr(cell, "interface", None)
            if host is None:  # fall back to "node[iface]" string form
                s = str(cell)
                host, _, rest = s.partition("[")
                iface = rest.rstrip("]")
            return str(host), str(iface)

        edges = []
        for r in df.to_dict(orient="records"):
            n1, i1 = side(r["Interface"])
            n2, i2 = side(r["Remote_Interface"])
            edges.append({"n1": n1, "i1": i1, "n2": n2, "i2": i2})
        return edges

    # -- best paths / RIB slice ---------------------------------------------------
    def routes_to(self, network: str, snapshot: str, prefix: str,
                  nodes: str | None = None) -> dict:
        """Main-RIB routes matching a prefix (optionally per node)."""
        bf = self._session(network, snapshot)
        q = bf.q.routes(network=prefix, nodes=nodes) if nodes else bf.q.routes(network=prefix)
        df = q.answer().frame()
        return {"route_count": int(len(df)), "routes": _records(df)}
