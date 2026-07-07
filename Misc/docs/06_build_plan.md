# 06 — Build Plan (ordered steps for Claude Code)

Do these in order. Each step is independently verifiable.

## Step 0 — Confirm the unknowns (blocking)
- Clone `Presidio-Federal/batfish-mcp-container`.
- Resolve the two schema unknowns in docs/03 (failure_impact multi-failure?
  check_routing session+bestpath?).
- Confirm the MCP transport (HTTP endpoint path) your client will use.
Update CLAUDE.md "open items" with the answers.

## Step 1 — Stand up the stack
- Use `docker/docker-compose.yml` to bring up engine + MCP container.
- Verify both healthy (docker/README_docker.md has the checks).
- Call MCP `tools/list`; confirm the ~12 tools we need are present and note their
  EXACT names (may differ from source due to middleware).

## Step 2 — MCP client wrapper
- Flesh out `app/mcp_client.py`: connect, list tools, call a tool, parse the
  structured result. Handle the tool-name mapping from Step 1.
- Smoke test: init a snapshot from the 13-config lab, call get_parse_status,
  print result.

## Step 3 — Translator loop
- Implement the upload turn (docs/02): stage → finalize → parse-check → confirm.
- Implement scenario classification + tool selection using
  `prompts/translator_system_prompt.md`.
- Wire the LLM tool-calling loop to the MCP client.

## Step 4 — Snapshot ledger + stacking
- Implement the ledger (docs/04).
- Implement Case A (failure list) OR Case B (re-stage) based on Step 0's answer.
- Test: stack two changes, confirm the second builds on the first.

## Step 5 — Synthesizer + output contract
- Implement the two-zone answer using
  `prompts/synthesizer_system_prompt.md` and docs/05.
- Enforce: FINDINGS only contains tool-returned facts; VERDICT tags every step.

## Step 6 — Streamlit UI
- Flesh out `app/streamlit_app.py`: upload widget, chat loop, render two-zone
  answer, show snapshot ledger state in a sidebar.

## Step 7 — Validate on the lab
- Run the 13-config AS1/AS2/AS3 lab (sample scenarios in
  reference/sample_scenarios.md).
- Check verdicts against hand analysis for at least: a link failure with working
  failover (expect GO or GO-WITH-CONDITIONS), and a change that isolates the AS2
  dept prefix (expect NO-GO).

## Step 8 — Harden
- Tool-filtering middleware: hide the ~30 out-of-scope tools.
- Snapshot cleanup on session reset.
- Pin the MCP container version tag (don't track latest).
