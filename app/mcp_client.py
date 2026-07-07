"""
Thin MCP client wrapper for the Batfish MCP server.

Responsibilities:
  - connect to the MCP streamable-http endpoint (default http://localhost:3009/mcp/)
  - list tools (names confirmed live against the running container, 2026-07-06)
  - call a tool with validated args, return the structured result
  - scope the toolset via the x-mcp-tools header (upstream middleware)

Sync facade over the async `mcp` SDK: each public call runs its own short-lived
session on a private event loop. The Batfish engine holds all state
(networks/snapshots), so per-call sessions are safe and reconnect-free.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_ENDPOINT = os.environ.get("BATFISH_MCP_URL", "http://localhost:3009/mcp/")

# A few tools (network_traceroute, network_bidirectional_reachability) expose a
# `host` param that defaults to "localhost" INSIDE the MCP container and ignore
# BATFISH_HOST — pass the engine's docker service name explicitly.
BATFISH_ENGINE_HOST = os.environ.get("BATFISH_ENGINE_HOST", "batfish")

# Toolsets we expose (hides aws/compliance/github/testing — build plan step 8).
# The upstream ToolFilterMiddleware reads this from the x-mcp-tools header.
SCOPED_TOOLSETS = "initialization,management,batfish,network"


class MCPToolError(RuntimeError):
    """A tool call failed on the server. Never fabricate a result — surface it."""


@dataclass
class ToolInfo:
    name: str
    description: str
    input_schema: dict


class BatfishMCPClient:
    def __init__(self, endpoint: str = MCP_ENDPOINT, scope_tools: bool = True):
        self.endpoint = endpoint
        self.headers = {"x-mcp-tools": SCOPED_TOOLSETS} if scope_tools else None
        self.tools: dict[str, ToolInfo] = {}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=600)

    async def _with_session(self, fn):
        async with streamablehttp_client(self.endpoint, headers=self.headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return await fn(session)

    def list_tools(self) -> dict[str, ToolInfo]:
        async def go(session):
            result = await session.list_tools()
            return {
                t.name: ToolInfo(t.name, t.description or "", t.inputSchema or {})
                for t in result.tools
            }

        self.tools = self._run(self._with_session(go))
        return self.tools

    def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Invoke a tool, return its structured result (dict/list) or raw text."""

        async def go(session):
            return await session.call_tool(tool_name, arguments)

        result = self._run(self._with_session(go))
        if result.isError:
            texts = [c.text for c in result.content if getattr(c, "text", None)]
            raise MCPToolError(f"{tool_name}: {' '.join(texts) or 'tool error'}")
        if result.structuredContent is not None:
            return result.structuredContent
        texts = [c.text for c in result.content if getattr(c, "text", None)]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except (json.JSONDecodeError, ValueError):
            return joined


class BatfishOps:
    """Higher-level operations the orchestrator calls. Tool names are the REAL
    registered names from the running container's tools/list (confirmed)."""

    def __init__(self, client: BatfishMCPClient):
        self.c = client

    # --- snapshot lifecycle ---------------------------------------------
    def init_snapshot(self, network: str, snapshot: str, configs: dict[str, str]):
        """Direct init from {filename: config_text}. Used for upload AND re-stage."""
        return self.c.call(
            "initialize_snapshot",
            {"network": network, "snapshot": snapshot, "configs": configs},
        )

    # --- management / ledger --------------------------------------------
    def list_snapshots(self, network: str):
        return self.c.call("management_list_snapshots", {"network": network})

    def snapshot_info(self, network: str, snapshot: str):
        return self.c.call(
            "management_get_snapshot_info", {"network": network, "snapshot": snapshot}
        )

    def parse_status(self, network: str, snapshot: str):
        return self.c.call(
            "management_get_parse_status", {"network": network, "snapshot": snapshot}
        )

    def delete_snapshot(self, network: str, snapshot: str):
        return self.c.call(
            "management_delete_snapshot", {"network": network, "snapshot": snapshot}
        )

    def delete_network(self, network: str):
        return self.c.call("management_delete_network", {"network": network})

    # --- analysis (the query bundle) --------------------------------------
    def check_routing(self, network: str, snapshot: str, protocols: list[str]):
        """Health check only: protocol process presence, NOT sessions/best-path."""
        return self.c.call(
            "batfish_check_routing",
            {"network": network, "snapshot": snapshot, "protocols": protocols},
        )

    def simulate_traffic(self, network: str, snapshot: str, src: str, dst: str,
                         applications: list[str] | None = None):
        args = {"network": network, "snapshot": snapshot, "src": src, "dst": dst}
        if applications:
            args["applications"] = applications
        return self.c.call("batfish_simulate_traffic", args)

    def failure_impact(self, network: str, snapshot: str, failure_type: str, target: str):
        """failure_type: 'node' | 'interface'. Interface target format: node[iface].
        SINGLE failure only — stacked failures go through re-staged snapshots."""
        return self.c.call(
            "batfish_failure_impact",
            {"network": network, "snapshot": snapshot,
             "failure_type": failure_type, "target": target},
        )

    def traceroute(self, network: str, snapshot: str, source_location: str,
                   dest_ip: str, dest_port: int | None = None, ip_protocol: str = "tcp",
                   src_ip: str | None = None):
        args = {"network": network, "snapshot": snapshot,
                "source_location": source_location, "dest_ip": dest_ip,
                "ip_protocol": ip_protocol, "host": BATFISH_ENGINE_HOST}
        if dest_port is not None:
            args["dest_port"] = dest_port
        if src_ip:
            args["src_ip"] = src_ip
        return self.c.call("network_traceroute", args)

    def bidir_reach(self, network: str, snapshot: str, location_a: str, location_b: str,
                    ip_a: str, ip_b: str, port: int | None = None, protocol: str = "tcp"):
        args = {"network": network, "snapshot": snapshot,
                "location_a": location_a, "location_b": location_b,
                "ip_a": ip_a, "ip_b": ip_b, "protocol": protocol,
                "host": BATFISH_ENGINE_HOST}
        if port is not None:
            args["port"] = port
        return self.c.call("network_bidirectional_reachability", args)

    def analyze_acl(self, network: str, snapshot: str, acl_name: str | None = None):
        args = {"network": network, "snapshot": snapshot}
        if acl_name:
            args["acl_name"] = acl_name
        return self.c.call("network_analyze_acl_rules", args)

    def run_tests(self, network: str, snapshot: str, tags: list[str]):
        return self.c.call(
            "batfish_run_tagged_tests",
            {"network": network, "snapshot": snapshot, "tags": tags},
        )

    def topology(self, network: str, snapshot: str, include_hosts: bool = True):
        return self.c.call(
            "network_generate_topology",
            {"network": network, "snapshot": snapshot, "include_hosts": include_hosts},
        )
