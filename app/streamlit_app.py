"""
NetGuard-CIA — Streamlit chat UI.

Flow:
  - sidebar: upload configs -> upload turn (init snapshot / parse gate / info)
    + human-readable snapshot ledger
  - main: chat; answers render full-width as Verdict summary table ->
    engine-facts table -> FINDINGS/VERDICT prose -> before/after topology
  - a topology diagram renders when the base snapshot is built

Run:  streamlit run app/streamlit_app.py
Env:  BATFISH_MCP_URL (default http://localhost:3009/mcp/), OLLAMA_API_KEY
"""
from pathlib import Path

import pandas as pd
import streamlit as st

from engine_direct import DirectEngine
from mcp_client import BatfishMCPClient, BatfishOps
from orchestrator import Ledger, cleanup_session, run_scenario_turn, run_upload_turn
from ui_helpers import (before_after_figures, classify_devices, facts_rows,
                        parse_verdict, topology_figure, verdict_status,
                        verdict_summary_rows)

LOGO = Path(__file__).resolve().parent.parent / "logo.png"

st.set_page_config(page_title="NetGuard-CIA",
                   page_icon=str(LOGO) if LOGO.exists() else "🛡️",
                   layout="wide")
if LOGO.exists():
    st.logo(str(LOGO), size="large")

# --- brand styling (steel blue, from logo.png) -------------------------------
st.markdown("""
<style>
  h1 {
    background: linear-gradient(90deg, #1F4E79 0%, #2E6C9E 55%, #7BA7CC 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    font-weight: 800 !important;
  }
  [data-testid="stSidebar"] {
    background: linear-gradient(180deg, #F0F4F9 0%, #E3EBF4 100%);
    border-right: 1px solid #D5E0EC;
  }
  /* st.logo caps at ~32px — scale the brand mark up */
  [data-testid="stLogo"], [data-testid="stSidebarHeader"] img {
    height: 8.5rem !important;
    max-width: 90% !important;
    width: auto !important;
    object-fit: contain;
    margin: 0.4rem auto 0 auto;
    display: block;
  }
  [data-testid="stSidebarHeader"] {
    height: auto !important;
    padding-bottom: 0 !important;
  }
  [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {
    color: #1F4E79;
  }
  .stButton > button {
    border-radius: 10px;
    border: 1px solid #2E6C9E;
    color: #1F4E79;
    font-weight: 600;
  }
  .stButton > button:hover {
    background: #2E6C9E;
    color: white;
  }
  div[data-testid="stTable"] table {
    border-radius: 12px;
    overflow: hidden;
  }
  div[data-testid="stTable"] thead th {
    background: #1F4E79;
    color: white !important;
  }
  .verdict-banner {
    display: flex; align-items: center; gap: 14px;
    border-radius: 14px; padding: 16px 20px; margin: 6px 0 14px 0;
    font-size: 1.05rem; line-height: 1.45;
    border: 1px solid; border-left-width: 10px;
  }
  .verdict-banner .icon { font-size: 1.9rem; }
  .verdict-banner .label { font-weight: 800; font-size: 1.15rem; }
</style>
""", unsafe_allow_html=True)


# --- session state ----------------------------------------------------------
if "ledger" not in st.session_state:
    st.session_state.ledger = Ledger()
if "messages" not in st.session_state:
    st.session_state.messages = []
if "snapshot_ready" not in st.session_state:
    st.session_state.snapshot_ready = False
if "base_edges" not in st.session_state:
    st.session_state.base_edges = None


@st.cache_resource
def get_ops() -> BatfishOps:
    client = BatfishMCPClient()
    client.list_tools()  # fail fast if the stack isn't up
    return BatfishOps(client)


@st.cache_resource
def get_engine() -> DirectEngine:
    return DirectEngine()


def decode_uploads(files) -> dict[str, str]:
    return {f.name: f.getvalue().decode("utf-8", errors="replace") for f in files}


def render_ledger(ledger: Ledger) -> None:
    """Human-readable session state instead of raw JSON."""
    if not ledger.base:
        st.caption("No network loaded yet — upload configs to begin.")
        return
    chain = " → ".join(["original"] + ledger.chain) or "original"
    current = "original" if ledger.current == ledger.base else ledger.current
    st.markdown(f"**Timeline:** {chain}")
    st.markdown(f"**Current view:** `{current}`")
    fs = ledger.node_failures or ledger.interface_failures
    if fs:
        st.markdown("**Active failures:**")
        for n in ledger.node_failures:
            st.markdown(f"- 🔴 node `{n}`")
        for i in ledger.interface_failures:
            st.markdown(f"- 🔌 interface `{i}`")
    else:
        st.markdown("**Active failures:** none")
    if ledger.edits:
        st.markdown("**Change history:**")
        for k, e in enumerate(ledger.edits, 1):
            st.markdown(f"{k}. {e}")
    else:
        st.markdown("**Change history:** none")


def render_answer(result, topo=None, live=True) -> tuple[str, dict | None]:
    """Render one scenario answer (verdict table, facts, prose, topology) with
    full Streamlit formatting. Returns (markdown, topo_snapshot). Called live for
    the current turn AND on every rerun to re-render history richly (Streamlit
    replays the whole script, so past turns must be re-rendered from their stored
    `result`, not flattened to markdown). `live=False` replays a past turn: it
    reuses the stored `topo` snapshot instead of re-querying the engine (which
    would be slow and reflect the CURRENT, not that turn's, network state)."""
    parts: list[str] = []

    is_query = getattr(result, "mode", "change") == "query"
    zone_label = "answer" if is_query else "verdict"
    fields = parse_verdict(result.verdict or "")

    # colored banner (status color + icon + label — never color alone)
    label, icon, accent, text_col = verdict_status(fields)
    headline = ((fields.get("VERDICT") or fields.get("ANSWER") or "")
                .split("\n")[0])
    st.markdown(
        f'<div class="verdict-banner" style="border-color:{accent};'
        f'background:{accent}18;color:{text_col}">'
        f'<span class="icon">{icon}</span>'
        f'<span><span class="label">{label}</span><br/>{headline}</span></div>',
        unsafe_allow_html=True,
    )
    parts.append(f"### {icon} {label}\n\n{headline}")

    rows = verdict_summary_rows(fields)
    if rows:
        glance = "Answer at a glance" if is_query else "Verdict at a glance"
        st.markdown(f"#### {glance}")
        df = pd.DataFrame(rows, columns=["", "Value"]).set_index("")
        st.table(df)
        parts.append(f"**{glance}**\n\n"
                     + "\n".join(f"- **{k}:** {v}" for k, v in rows))

    frows = facts_rows(result.tool_log)
    if frows:
        st.markdown("#### Network checks performed")
        # st.table wraps long cell text (st.dataframe truncates it)
        st.table(pd.DataFrame(frows).set_index("Check"))
        parts.append("**Network checks:** "
                     + "; ".join(f"{r['Check']} [{r['Target']}] = {r['Result']}"
                                  for r in frows[:8]))

    if result.findings:
        st.markdown("#### Findings")
        st.markdown(result.findings)
        parts.append(f"**Findings**\n\n{result.findings}")
    st.markdown(f"#### Full {zone_label}")
    st.markdown(result.verdict)
    parts.append(f"**{zone_label.capitalize()}**\n\n{result.verdict}")

    # before/after topology when the scenario changed the network. This builds
    # matplotlib figures (expensive), so only do it on the LIVE turn — NOT on
    # every history replay, which would regenerate N figures on every rerun and
    # crawl. History keeps its (cheap) banner/tables/findings; the diagram is
    # shown for the turn you just ran.
    topo_out = topo
    ledger = st.session_state.ledger
    if live and ledger.current != ledger.base and st.session_state.base_edges is not None:
        try:
            topo_out = {"cur_edges": get_engine().layer3_edges(
                            ledger.network, ledger.current),
                        "failed_nodes": list(ledger.node_failures)}
            before, after = before_after_figures(
                st.session_state.base_edges, topo_out["cur_edges"],
                failed_nodes=topo_out.get("failed_nodes"),
                device_types=st.session_state.get("device_types"))
            st.markdown("#### Topology — before vs after")
            c1, c2 = st.columns(2)
            with c1:
                st.pyplot(before, width="stretch")
            with c2:
                st.pyplot(after, width="stretch")
            st.caption("Dashed red = links lost vs the original network; "
                       "red devices = failed.")
            import matplotlib.pyplot as _plt   # free the figures (avoid leak)
            _plt.close(before)
            _plt.close(after)
        except Exception as e:
            st.caption(f"(topology diagram unavailable: {e})")
            topo_out = None

    with st.expander(f"Analysis details — advanced ({len(result.tool_log)} checks)"):
        for entry in result.tool_log:
            flag = " ⚠️" if entry["is_error"] else ""
            st.markdown(f"**{entry['tool']}**{flag}")
            st.json({"input": entry["input"]})
            st.text(entry["result"][:2000])

    return "\n\n".join(parts), topo_out


# --- sidebar ----------------------------------------------------------------
with st.sidebar:
    st.subheader("1) Upload configs")
    files = st.file_uploader("Cisco IOS / Juniper Junos configs",
                             accept_multiple_files=True, key="config_uploader")
    # Cache decoded configs in session state: the uploader widget can lose its
    # files across st.rerun() (e.g. after Reset session), and the cache also
    # lets you rebuild after a reset without re-uploading.
    if files:
        st.session_state.pending_configs = decode_uploads(files)
    pending = st.session_state.get("pending_configs") or {}
    if pending:
        st.caption(f"{len(pending)} config file(s) ready: "
                   + ", ".join(sorted(pending)[:4])
                   + (" …" if len(pending) > 4 else ""))
    if st.button("Build snapshot", disabled=not pending):
        with st.spinner("Staging snapshot + parse check..."):
            try:
                ops = get_ops()
                ok, msg = run_upload_turn(ops, st.session_state.ledger,
                                          pending)
            except Exception as e:  # stack down, connection refused, ...
                ok, msg = False, f"Could not reach the Batfish stack: {e}"
        st.session_state.snapshot_ready = ok
        (st.success if ok else st.error)(msg)
        if ok:
            st.session_state.device_types = classify_devices(
                st.session_state.ledger.configs)
            try:
                st.session_state.base_edges = get_engine().layer3_edges(
                    st.session_state.ledger.network, "base")
            except Exception:
                st.session_state.base_edges = None
            st.session_state.messages.append(
                {"role": "assistant", "content": msg})

    # NOTE: the snapshot ledger + reset button are rendered at the END of the
    # script (bottom of this file) so they reflect ledger mutations made by
    # the scenario turn during THIS run — rendering them here would always
    # show the pre-turn state.


# --- main chat --------------------------------------------------------------
st.title("Network Change-Impact — What-If")

if not st.session_state.snapshot_ready:
    st.info("Upload configs and build a snapshot to begin. Once the model parses "
            "cleanly, ask a what-if scenario in plain English.")
elif st.session_state.base_edges:
    with st.expander("Network topology",
                     expanded=not st.session_state.messages):
        left, _ = st.columns([3, 1])   # keep the diagram a readable size
        with left:
            st.pyplot(topology_figure(st.session_state.base_edges,
                                      device_types=st.session_state.get("device_types"),
                                      title="YOUR NETWORK"),
                      width="stretch")

for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        # re-render past assistant answers with full formatting (banner, tables,
        # topology) from their stored result — plain markdown would flatten them
        if m["role"] == "assistant" and m.get("result") is not None:
            render_answer(m["result"], topo=m.get("topo"), live=False)
        else:
            st.markdown(m["content"])

prompt = st.chat_input("e.g. What breaks if I shut the AS1–AS3 link?",
                       disabled=not st.session_state.snapshot_ready)
if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        status = st.status("Translating scenario...", expanded=True)
        try:
            result = run_scenario_turn(
                get_ops(), st.session_state.ledger, prompt,
                progress=lambda msg: status.write(msg),
            )
        except Exception as e:
            status.update(label="Failed", state="error")
            st.error(f"Scenario turn failed: {e}")
            st.stop()
        status.update(label="Done", state="complete", expanded=False)

        if result.clarification:
            st.markdown(result.clarification)
            msg = {"role": "assistant", "content": result.clarification}
        else:
            rendered, topo = render_answer(result)
            msg = {"role": "assistant", "content": rendered,
                   "result": result, "topo": topo}

    st.session_state.messages.append(msg)


# --- sidebar: session state + reset (rendered LAST -> shows post-turn state) --
with st.sidebar:
    st.subheader("Session state")
    render_ledger(st.session_state.ledger)

    if st.button("Reset session"):
        try:
            cleanup_session(get_ops(), st.session_state.ledger)
        except Exception:
            pass  # cleanup is best-effort; engine restart clears snapshots anyway
        st.session_state.ledger = Ledger()
        st.session_state.messages = []
        st.session_state.snapshot_ready = False
        st.session_state.base_edges = None
        # keep pending_configs: uploaded files survive the reset so you can
        # click "Build snapshot" again immediately (or upload a new set).
        st.rerun()
