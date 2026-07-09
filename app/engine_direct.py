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
from pybatfish.datamodel.primitives import Interface

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
        return out

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
