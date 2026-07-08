# NetGuard-CIA — How a Question Becomes a Verdict

*An end-to-end walkthrough with real code, real scenarios from the `scenarios/`
folder, and the actual outputs the app produced while we validated it. Read
time: ~25 minutes.*

---

## 1. The cast, and the one rule

Five things cooperate to answer a what-if question:

| Piece | File | Job |
|---|---|---|
| **Streamlit UI** | `app/streamlit_app.py` | Upload, chat, verdict tables, topology diagrams |
| **Orchestrator** | `app/orchestrator.py` | The upload turn, the translator loop, the session ledger, the synthesizer call |
| **Translator LLM** | `qwen3-coder:480b` via Ollama Cloud | Reads your question, decides which checks to run, with what parameters |
| **Analysis engine** | Batfish (Docker) via `app/mcp_client.py` + `app/engine_direct.py` | Computes every network fact deterministically |
| **Synthesizer LLM** | `gpt-oss:120b` via Ollama Cloud | Reads the raw check results and writes the two-zone verdict |

The one rule that shapes everything: **the LLMs never compute a network
fact.** The translator only *chooses and parameterizes* checks; the
synthesizer only *summarizes and judges* what the checks returned. Anything in
an answer that didn't come from an engine result is, by definition, a bug.

```
your question ──► TRANSLATOR ──► engine checks ──► SYNTHESIZER ──► verdict
                  (which checks?)  (the facts)      (what do the
                                                     facts mean?)
```

---

## 2. Stage 0 — the upload turn (no AI involved)

When you upload configs and click **Build snapshot**, no LLM runs at all.
`run_upload_turn()` in `orchestrator.py` is plain deterministic code:

```python
def run_upload_turn(ops, ledger, configs):
    ops.init_snapshot(ledger.network, "base", configs)     # 1. build the model

    ps = ops.parse_status(ledger.network, "base")          # 2. THE PARSE GATE
    if ps.get("errors"):
        return False, ("Parse FAILED — a verdict on a half-parsed config "
                       "is worse than no verdict. ...")

    info = ops.snapshot_info(ledger.network, "base")       # 3. device inventory

    ledger.base = ledger.current = "base"                  # 4. reset the ledger
    ledger.node_failures = []
    ledger.interface_failures = []
    ...
    ledger.configs = dict(configs)                         # 5. keep raw text
    ledger.device_summary = json.dumps({"nodes": nodes, ...})
```

Three things worth noticing:

- **The parse gate (step 2) is a hard stop.** If any config fails to parse,
  you get an error and no chat. The reasoning: a confidently wrong answer
  about a half-understood network is the worst possible outcome.
- **The ledger is born here.** The `Ledger` dataclass is the app's memory
  across chat turns — which network states exist, which failures are active,
  what's been edited. The sidebar's "Session state" panel is this object.
- **The raw config text is kept** (`ledger.configs`) so the translator can
  later read a device's config and produce a precise edit.

For **scenario 1** (the 13 city routers), this step produced:

```
Network loaded: 13 devices (CISCO_IOS), all configs parsed cleanly.
Ask me a what-if scenario.
```

…and the UI then asks the engine for the layer-3 adjacencies
(`engine_direct.layer3_edges`) to draw the "Network topology" diagram — real
modeled links, not a guess.

---

## 3. Stage 1 — the translator loop (how checks get chosen)

When you send a chat message, `run_scenario_turn()` starts a **tool-use
loop**. The translator model receives three things:

1. **A system prompt** (`prompts/translator_system_prompt.md` + a runtime
   addendum) with binding rules, e.g.:

   > *FAILURES (single or stacked): your FIRST tool call MUST be
   > `apply_failure_set` whenever the user fails/shuts anything — it is the
   > only action that records the failure in the session ledger and creates
   > the failed snapshot your probes run against.*

   > *VANTAGE POINTS: after failing a link/interface on device X, do NOT
   > judge reachability only from X itself — its local view is often the
   > outlier. Probe from at least one interior device behind it too.*

2. **The current ledger** (so "now also…" means something), and
3. **The device inventory** (real node and interface names, so it can't
   invent devices).

It also gets ~13 **tool definitions** — JSON schemas describing each check.
The loop itself (simplified from `orchestrator.py`):

```python
messages = [{"role": "system", "content": _translator_system(ledger)},
            {"role": "user", "content": user_text}]

for _ in range(MAX_TRANSLATOR_ITERATIONS):          # hard cap: 12 rounds
    response = client.chat.completions.create(
        model=TRANSLATOR_MODEL, tools=_openai_tools(), messages=messages)
    tool_calls = response.choices[0].message.tool_calls or []
    if not tool_calls:
        final_text = msg.content            # done investigating
        break

    for tc in tool_calls:
        args = json.loads(tc.function.arguments)
        out = _execute_translator_tool(ops, ledger, tc.function.name, args)
        tool_log.append({"tool": name, "input": args, "result": out, ...})
        messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
```

Every call and its raw result is appended to `tool_log` — this list later
becomes both the **"Network checks performed" table** in the UI and the
**only input** the synthesizer is allowed to reason from.

`_execute_translator_tool()` is the dispatcher that maps a tool name to real
work — some calls go to the MCP server, some go directly to the engine via
pybatfish, and two ( `apply_failure_set`, `stage_change_snapshot` ) also
mutate the ledger.

---

## 4. Walkthrough A — Scenario 1: *"What breaks if the london router fails?"*

From `scenarios/scenario 1 - failure impact and chaos monkey/QUESTIONS.txt`:

> "What breaks if the london router fails entirely? I care about whether
> paris can still reach the PoP prefix 2.128.0.0/24."

Here is the **actual tool sequence** from our validation run, verbatim:

```
TOOLS: ['apply_failure_set', 'batfish_simulate_traffic',
        'network_traceroute', 'batfish_check_routing',
        'network_analyze_acl_rules']
VERDICT LINE: VERDICT: GO — Paris retains connectivity to the PoP prefix
              2.128.0.0/24 despite the London router failure.
```

### Step A1 — `apply_failure_set(node_failures=["london"])`

The dispatcher records the failure in the ledger and asks the engine to
**fork** the network state with that device deactivated:

```python
# orchestrator.py (dispatcher)
if name == "apply_failure_set":
    for n in args.get("node_failures") or []:
        ledger.node_failures.append(n)
    fork = ledger.apply_failure_fork(_ENGINE)      # -> "fail1"

# Ledger.apply_failure_fork
def apply_failure_fork(self, engine):
    base = self.edit_snapshot or self.base         # fork from latest EDIT state
    name = f"fail{self._fork_seq}"
    engine.fork_with_failures(self.network, base, name,
                              self.node_failures, self.interface_failures)
    self.current = name                            # probes now default here
```

```python
# engine_direct.py — the actual engine call (pybatfish)
result = bf.fork_snapshot(
    base_name=base_snapshot, name=name, overwrite=True,
    deactivate_nodes=node_failures or None,        # takes LISTS →
    deactivate_interfaces=ifaces or None,          # stacking is one fork
)
```

Key idea: a fork is a **copy of the modeled network with elements switched
off** — no config editing, no re-parsing, near-instant. Because it takes the
*whole* accumulated failure set each time, "now also fail X" is just one more
call. The ledger after this step:

```json
{"chain": ["fail1"], "current": "fail1",
 "failure_set": {"nodes": ["london"], "interfaces": []}}
```

### Step A2 — probes against the failed state

Every check the translator now runs defaults to `ledger.current` — i.e. the
world *with London dead*. The decisive one:

```python
# network_traceroute(source_location="paris", dest_ip="2.128.0.1")
# → runs Batfish's traceroute question on snapshot "fail1"
```

The engine returns the actual forwarding decision, hop by hop — for this
network, Paris still reaches the PoP via the surviving ring. It also ran a
routing-process check and an ACL analysis as corroboration. Note what the
translator did *not* do: it never "figured out" the path itself. It asked;
the engine computed.

### Step A3 — hand-off

When satisfied, the translator stops calling tools and emits
`READY FOR SYNTHESIS` plus a short note on which results matter. It is
explicitly forbidden from writing the verdict — that separation exists so the
model that *chose* the probes can't also *grade* them.

---

## 5. Walkthrough B — Scenario 2, two turns: stacking to a NO-GO

From `scenarios/scenario 2 - link failure with failover/QUESTIONS.txt`. This
is the walkthrough that shows the whole machine, because the correct answer
**flips between turns**.

### Turn 1 — *"What breaks if I shut the AS1-AS2 direct link (as1border1 GigabitEthernet1/0)? I care about AS1 reaching 2.128.0.0/16."*

Actual run:

```
TOOLS: ['apply_failure_set', 'batfish_simulate_traffic', 'network_traceroute',
        'differential_reachability', 'apply_failure_set',
        'differential_reachability', 'network_traceroute', 'network_traceroute']
VERDICT: GO — AS1 can still reach the 2.128.0.0/16 subnet after shutting
         the direct AS1-AS2 link.
```

The engine's traceroute from the interior router `as1core1` on the failed
state shows the money fact — the traffic didn't just have a theoretical
backup, it **actually selected** the AS3 transit path:

```
base : as1core1 → as1border1 → as2border1 → as2core1 → as2dist1 → as2dept1
fail1: as1core1 → as1border2 → as3border2 → as3core1 → as3border1
                → as2border2 → as2core2 → as2dist2 → as2dept1     (all ACCEPTED)
```

That distinction is encoded in the synthesizer's rules:

> *A "path exists" finding is NOT a working failover unless an analysis
> result shows it SELECTED/used after the change.*

The translator also ran `differential_reachability` — the engine-native diff
of the two states. The code compresses each changed flow into a readable
before/after pair:

```python
# engine_direct.py
ans = bf.q.differentialReachability().answer(
    snapshot=after, reference_snapshot=before)
...
{"flow": "start=as1border1 [1.1.1.1→10.12.11.0 ...]",
 "before": "EXITS_NETWORK", "after": "NO_ROUTE"}
```

Result in this run: **47 flows changed disposition** — flows to the
department prefix survived (rerouted), while flows to the dead link's own
subnet (`10.12.11.0/24`) went `NO_ROUTE`, exactly as you'd expect when a link
dies. Both facts land in the findings; neither is fatal to the service asked
about → **GO**.

### Turn 2 — *"Now ALSO shut the AS1-AS3 link (as1border2 GigabitEthernet0/0). Can AS1 still reach 2.128.0.1?"*

This is where stacking earns its keep. Actual run:

```
TOOLS: ['apply_failure_set', 'batfish_failure_impact',
        'differential_reachability', 'network_traceroute', 'network_traceroute']
VERDICT: NO-GO — AS1 cannot reach 2.128.0.1 after shutting both
         AS1-AS2 and AS1-AS3 links   (Confidence: High)
```

The single `apply_failure_set` call passed only the *new* interface, but the
ledger already held the first one — so the fork deactivated **both**:

```json
{"chain": ["fail1", "fail2"], "current": "fail2",
 "failure_set": {"interfaces": ["as1border1[GigabitEthernet1/0]",
                                 "as1border2[GigabitEthernet0/0]"]}}
```

Traceroutes from **two different vantage points** (`as1core1` *and*
`as1border1`) both returned `NO_ROUTE`, and the before/after comparison
listed the same flows as broken. Multi-source agreement is what let the
synthesizer say NO-GO with High confidence — the reasoning it produced:

> - traceroute probes from both as1core1 and as1border1 report NO_ROUTE →
>   **[verified]** the specific service flow is unreachable.
> - the before/after comparison lists the same flows now NO_ROUTE →
>   **[verified]** confirms loss across the network model.
> - although the blast-radius check returned NO_IMPACT, that is a
>   coarse-grained aggregation; the flow-specific evidence overrides it per
>   the conflict rule → **[judgment]**.

Note the last bullet: two engine results *disagreed*, and the system has a
written rule for that (see §8).

---

## 6. Walkthrough C — Scenario 4-style: *"Are there any problems with the current configs?"*

No change is being proposed here, so no forks — the translator reaches for
the health bundle and the session check:

```python
# engine_direct.py — health_checks() runs five engine questions:
bf.q.initIssues()                 # parse/conversion problems
bf.q.undefinedReferences()        # config refers to something undefined
bf.q.interfaceProperties(...)     # → duplicate primary IPs across devices
bf.q.bgpSessionStatus()           # per-neighbor session establishment
bf.q.detectLoops()                # forwarding loops
```

Against scenario 2's lab (which ships with defects planted on purpose), the
real output found every seeded problem:

```
init_issues: 1   undefined_references: 1   duplicate_interface_ips: 2
duplicated addresses: ['2.1.1.2/32']        ← the planted duplicate loopback
bgp not-established: 3                       ← the planted dead peers
forwarding loops: 0
```

`bgpSessionStatus` deserves a note: the summary
(`{'ESTABLISHED': 34, 'NOT_COMPATIBLE': 3}`) is *modeled* session state —
what the configs imply sessions would do — which is why verdicts may cite it
as `[verified]` while still listing **live** session state as unknown.

---

## 7. Stage 2 — the synthesizer (how the verdict gets written)

Once the translator stops, the orchestrator packages **everything the engine
said** and nothing else:

```python
synth_user = f"""SCENARIO (user's words): {user_text}

LEDGER: {json.dumps(ledger.to_public_dict())}

TRANSLATOR NOTE: {final_text or '(none)'}

STRUCTURED TOOL RESULTS (the ONLY source of engine facts):
{engine_facts}                       # the full tool_log as JSON
"""
synth = client.chat.completions.create(
    model=SYNTHESIZER_MODEL,
    messages=[{"role": "system", "content": _synthesizer_system()},
              {"role": "user", "content": synth_user}])
```

The synthesizer's system prompt (`prompts/synthesizer_system_prompt.md`) is a
contract, not a suggestion. The load-bearing parts:

**Two zones.** Findings (only what checks returned, each line traceable to a
check) must be visually separate from the verdict (the judgment).

**Provenance tags.** Every reasoning step carries `[verified]`,
`[vendor-doc]`, or `[judgment]` — and *"confidence is capped by the weakest
provenance in the chain. If any critical step is [judgment], confidence is
not High."*

**Decision logic — first match sets the floor:**

```
1. INSUFFICIENT-DATA — device/peer missing, or a verdict-critical fact
   lacked verified backing
2. NO-GO            — breaks a named service with no verified failover,
                      or creates a loop/blackhole, or is hard to revert
3. GO-WITH-CONDITIONS — safe only with listed preconditions
4. GO               — no named-service loss, failover verified, no loops
```

**Plain language.** A translation table forbids internal words (tool names,
"Batfish", "engine", snapshot IDs) in the output — checks are named "path
trace", "failure simulation", "before/after comparison" and states are "the
original network" / "after the change".

**Mandatory residual-unknowns.** Every verdict must end by listing what
config-only analysis cannot see.

The reply is split mechanically on the `VERDICT:` marker:

```python
idx = answer.find("VERDICT:")
findings, verdict = answer[:idx].strip(), answer[idx:].strip()
```

---

## 8. Stage 3 — rendering, and the guardrails that earn the trust

### What you see in the UI (`ui_helpers.py`)

- `parse_verdict()` splits the verdict text into fields (tolerant of the
  models' markdown quirks — it even normalizes the Unicode hyphens some
  models emit inside `PACKET-FLOW`), feeding the **"Verdict at a glance"**
  table and the color-coded banner (`verdict_status()` maps GO→green ✅,
  NO-GO→red ⛔, conditions→amber ⚠️, insufficient→grey ❔).
- `facts_rows()` builds the **"Network checks performed"** table straight
  from `tool_log` — deterministically, no LLM — so this table *cannot* drift
  from what the engine actually returned.
- The **before/after topology** is drawn from `layer3_edges()` on the base
  vs. current state: links present in base but missing now render dashed red;
  failed devices fill red.

### Guardrails — each one exists because a real run went wrong

These aren't theoretical. Each was added after validation caught a failure:

**The fragment guard.** A model once passed just the changed stanza
(`interface GigabitEthernet1/0\n shutdown`) as the "full file" to
`stage_change_snapshot`, silently gutting the device in the model. Now:

```python
if len(text) < 0.5 * old_len:
    return (f"ERROR: {fn} shrank from {old_len} to {len(text)} chars — you "
            "must supply the COMPLETE file... Ledger NOT advanced.")
```

In the very next validated run the guard fired, the model re-read the config
and resubmitted correctly — the recovery loop works.

**The vantage rule.** Early runs produced a wrong NO-GO because the model
tracerouted only *from the device whose own uplink it had just shut* — that
device's view said NO_ROUTE while interior traffic rerouted fine. The rule
(probe from an interior device too) now lives in the tool descriptions *and*
the system prompt, and the fix flipped the validation verdict from an
incorrect NO-GO to the correct GO.

**The conflict rule.** When the blast-radius matrix says NO_IMPACT but a
*single-source* probe says NO_ROUTE, the evidence conflicts — the binding
rule forbids concluding NO-GO (or GO) from the lone probe; the floor becomes
INSUFFICIENT-DATA unless multiple sources agree. You can see it applied in
Walkthrough B, turn 2 — where multi-source agreement *did* exist, so NO-GO
stood.

**The parse gate & the no-advance rule.** Broken uploads never reach chat;
broken edits never advance the ledger.

**State hygiene.** Engine state outlives app restarts, so building a base
snapshot deletes stale `failN`/`changeN` leftovers, and "Reset session"
prunes the chain.

---

## 9. Limitations and caps — read this before trusting a verdict

### Hard caps in the code

| Cap | Value | Where | Consequence |
|---|---|---|---|
| Translator rounds | 12 | `MAX_TRANSLATOR_ITERATIONS` | A very complex scenario could hit the cap mid-investigation; synthesis then runs on whatever was gathered |
| Single check result fed to the LLM | 20,000 chars | `MAX_RESULT_CHARS` | Long results are truncated with a marker; the full text still shows in "Analysis details" |
| Total facts fed to synthesizer | 150,000 chars | `run_scenario_turn` | Very chatty investigations get their tool log truncated |
| Rows returned per engine table | 100 (40 for health rows) | `engine_direct.MAX_ROWS` | On big networks, listings (routes, changed flows) are samples, not exhaustive |
| Changed-flow detail | dispositions only | `differential_reachability` | Full hop-by-hop traces of changed flows are summarized to before/after dispositions |

### Semantic limits (the important ones)

- **Config-only, always.** The engine models what the configurations imply.
  It cannot see live traffic levels, real-time session state, convergence
  timing, hardware/optical faults, or anything an operator changed outside
  the files you uploaded. This is why RESIDUAL-UNKNOWNS is mandatory — treat
  a GO as *"the configs support this change"*, not *"nothing can go wrong"*.
- **`NO_IMPACT` has a narrow meaning.** The blast-radius check compares
  auto-generated flows; NO_IMPACT means "no previously-working flow broke" —
  a path that *changed* but still works counts as no impact. Path changes
  come from traceroute diffs.
- **"BGP session check" is modeled, not observed.** It tells you what the
  configs make possible — a peer that's down in real life for a hardware
  reason will still show ESTABLISHED in the model.
- **The routing process check is presence-only.** It confirms BGP/OSPF are
  configured, nothing more; the prompts explicitly demote it.
- **Traffic simulation needs a real destination.** A bare prefix like
  `2.128.0.0/16` as a destination fails ("node not found"); the tool docs
  steer the model toward a host IP + traceroute instead.
- **Host files aren't supported in upload** — only device configs. (This is
  why one upstream example network, forwarding-change-validation, was left
  out of the demo scenarios.)

### LLM-dependence (the honest part)

- **Probe selection is model judgment.** The translator decides which checks
  to run. The guardrails above force the big invariants (failures must be
  applied before probing; diffs must come from the comparison check), but a
  weak investigation is still possible — that's why the checks table is shown
  and the raw results are one click away.
- **Output language is prompt-enforced.** The plain-language rules and the
  two-zone format held in every validated run, but they are instructions to a
  model, not code. A stray internal term can leak.
- **Model tier.** The current Ollama Cloud key runs `qwen3-coder:480b` and
  `gpt-oss:120b` (the tool-capable models on the free tier; stronger models
  are subscription-gated). Both are swappable via env vars.

### Operational

- One user / one session at a time; the session ledger lives in the browser
  session. Restarting the engine container clears all network states —
  rebuild via the sidebar.
- Rich tables/diagrams render on the live turn; older chat messages persist
  as text summaries.
- A scenario turn takes ~1–3 minutes (several engine computations plus two
  LLM calls); the blast-radius matrix is the slow one.
- **Privacy:** configs are parsed locally in Docker and never leave your
  machine. What *does* go to the LLM endpoint: your question, the (truncated)
  check results, and the device inventory. Point `OLLAMA_BASE_URL` at a local
  model if that's not acceptable.

---

## 10. The 60-second recap

1. **Upload** → engine parses configs into a model; a parse gate blocks bad
   input; the ledger and topology diagram are born. *(No AI.)*
2. **You ask** → the **translator** LLM picks checks under binding rules:
   failures must go through `apply_failure_set` (one engine fork of the whole
   accumulated failure set — that's why "now also fail X" stacks), edits
   through the guarded `stage_change_snapshot`, diffs through the engine's
   native before/after comparison.
3. **The engine computes** every fact; every call + result is logged.
4. **The synthesizer** LLM gets *only* that log and must write a two-zone,
   provenance-tagged verdict under a fixed decision logic, ending with what
   config-only analysis can't see.
5. **The UI** renders the banner and tables deterministically from the same
   log, plus before/after topology from the engine's own adjacency data.

The trust story in one sentence: *the facts are computed by a deterministic
engine, the judgment is labeled as judgment, and every layer in between is
either plain code or a rule that was added because a real validation run
failed without it.*
