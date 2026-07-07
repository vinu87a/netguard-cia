# 04 — Snapshot Stacking

> **2026-07-08 UPDATE — Case A is implemented.** The MCP server exposes no fork
> tool, but the app now calls pybatfish directly (`app/engine_direct.py`):
> `fork_snapshot(deactivate_nodes=[...], deactivate_interfaces=[...])` accepts
> LISTS, so stacked FAILURES are one cheap fork per turn via the
> `apply_failure_set` translator tool (ledger keeps the cumulative set).
> The re-stage approach below remains the path for config/policy EDITS, and
> the two compose: after each new edit snapshot the active failure set is
> automatically re-forked on top. The original analysis follows.

## The problem
Users stack changes: "what if I shut link 1" then "now ALSO fail link 2" then
"now ALSO raise this local-pref." Each new scenario's baseline is the
already-changed network, not the original. Correct stacking requires each layer
to build on the previous.

There is **no `fork_snapshot` tool** in this MCP server (the tool list has init,
finalize, list, get-info, delete, and a staging pipeline — no clone-and-edit). So
we implement stacking two ways depending on scenario type.

## The ledger (LLM-held state)
A small structure the LLM maintains across turns:
```
ledger = {
  base:        "<snapshot name>",          # original parsed configs
  chain:       ["change1", "change2", …],  # ordered change snapshots
  current:     "<snapshot name>",          # what "now also …" builds on
  edits:       [ … cumulative edit deltas … ]  # for re-stage rebuilds
}
```

## Case A — failure-only stacking (cheap IF the tool allows lists)
If `failure_impact_tool` accepts multiple simultaneous failures (CONFIRM — see
docs/03), then stacked failures don't need new snapshots at all: accumulate the
failure set in the ledger and pass the whole set to `failure_impact_tool` against
`base` each turn. "Also fail link 2" = add link 2 to the set, re-call. Cheapest
path. This is why confirming that schema is the first build task.

If the tool does NOT accept multiple failures → treat failures like edits (Case
B), representing each downed link as a config edit (interface shutdown) in the
re-stage set.

## Case B — edit stacking (re-stage approach)
For config/policy edits (and failures, if the tool can't take a list):

1. Keep the cumulative edit set in `ledger.edits` (e.g. "as1border1 Gi1/0
   shutdown", "as2border1 route-map as2_to_as1 seq2 set local-preference 150").
2. On each new scenario turn, produce a fresh config set = original configs with
   ALL edits-so-far applied.
3. Re-stage: `network_prepare_snapshot` → upload the edited config set →
   `network_finalize_snapshot` as a new named snapshot `changeN`.
4. Run the query bundle against `changeN`; diff vs `current`.
5. Update ledger: append `changeN` to chain, set `current = changeN`.

This is heavier (a full re-stage per turn) but preserves exact stacking
semantics. For the 13-config lab the cost is negligible; for large networks
consider caching the edited config set.

## Applying an edit to a config
The LLM edits the raw config text (it has the originals from the upload). Example
deltas it must be able to express:
- Interface shutdown: add `shutdown` under the interface stanza.
- Local-pref change: modify the `set local-preference` line in the named
  route-map / policy-statement clause.
- Neighbor add/remove: add/remove the `neighbor …` lines.
- ACL edit: insert/remove an ACL line at the right sequence.

The translator prompt instructs precise, minimal edits and echoes the exact delta
to the user before computing, so a mis-edit is caught before it produces a
confident-but-wrong verdict.

## Diff direction
- Default: `changeN` vs `current-before-this-turn` = "what did this step break."
- On request: `changeN` vs `base` = "what has the whole sequence broken."

## Cleanup
Old change snapshots accumulate in the engine. Use `delete_snapshot_tool` to
prune the chain when a session ends or the user resets.
