"""
LLM provider abstraction: Commotion (Tata Communications AI worker) as primary
with Ollama Cloud (OpenAI-compatible) as fallback — or either alone.

The orchestrator speaks ONE dialect (OpenAI-style message dicts + tool
schemas); each adapter translates to its wire format. The full transcript is
always kept locally in that dialect, so a mid-turn failover replays cleanly
onto the fallback provider even though Commotion holds its own server-side
conversation state.

Commotion has no native function calling — the adapter emulates it: tool
schemas + a strict JSON reply protocol are embedded in the message text, the
reply is parsed for a {"tool": ..., "args": ...} object, and malformed replies
get up to two corrective retries before the provider is declared failed.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field

import httpx
from openai import OpenAI


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMReply:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)


class ProviderError(RuntimeError):
    """The provider failed for this call chain — caller may fall back."""


# ---------------------------------------------------------------------------
# Ollama Cloud / any OpenAI-compatible endpoint
# ---------------------------------------------------------------------------
class OpenAIProvider:
    name = "ollama"

    def __init__(self, base_url: str, api_key: str, translator_model: str,
                 synthesizer_model: str):
        if not api_key:
            raise ProviderError("OLLAMA_API_KEY not set")
        self._client = OpenAI(base_url=base_url, api_key=api_key)
        self._translator_model = translator_model
        self._synthesizer_model = synthesizer_model

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             role: str | None = None, format_hint: str | None = None) -> LLMReply:
        # role/format_hint are Commotion routing/format hints; Ollama uses the
        # system prompt directly, so they're ignored here (single call surface).
        model = self._translator_model if tools else self._synthesizer_model
        try:
            resp = self._client.chat.completions.create(
                model=model, max_tokens=16000, messages=messages,
                **({"tools": tools} if tools else {}))
        except Exception as e:
            raise ProviderError(f"ollama: {e}") from e
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(tc.id, tc.function.name, args))
        return LLMReply(content=msg.content or "", tool_calls=calls)


# ---------------------------------------------------------------------------
# Commotion (Tata Communications AI worker)
# ---------------------------------------------------------------------------
_JSON_RE = re.compile(r"\{.*\}", re.S)


class CommotionProvider:
    name = "commotion"

    def __init__(self, url: str, api_key: str, worker_id: str,
                 audience_id: str = "RPA", route_selector: str = "aicoe_workspace",
                 timeout: float = 120.0):
        if not (url and api_key and worker_id):
            raise ProviderError("COMMOTION_URL / COMMOTION_API_KEY / "
                                "COMMOTION_WORKER_ID not set")
        self._url, self._key = url, api_key
        self._worker, self._audience, self._route = worker_id, audience_id, route_selector
        self._timeout = timeout
        # incremental send: transcript identity -> (conversation_id, n_sent)
        self._sessions: dict[tuple, tuple[str, int]] = {}

    # -- wire ---------------------------------------------------------------
    def _post(self, text: str, conversation_id: str | None) -> dict:
        body = {"workerId": self._worker, "audienceId": self._audience,
                "messageText": text}
        if conversation_id:
            body["conversationId"] = conversation_id
        try:
            r = httpx.post(self._url, json=body, timeout=self._timeout,
                           headers={"X-Route-Selector": self._route,
                                    "Content-Type": "application/json",
                                    "apikey": self._key})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            raise ProviderError(f"commotion transport: {e}") from e
        if data.get("status") != "SUCCESS":
            raise ProviderError(f"commotion status={data.get('status')} "
                                f"error={data.get('errorMessage')!r}")
        return data

    # -- rendering the neutral transcript into message text ------------------
    # The worker owns all behavioral instructions (via its system prompt); the
    # app sends only data. For the translator we also include the machine-
    # generated check catalog, since it is generated from the live engine and
    # must stay in sync with the code.
    @staticmethod
    def _render(msg: dict, tools: list[dict] | None) -> str:
        role = msg["role"]
        if role == "system":
            # Pass the content through with its own SESSION STATE / DEVICE
            # INVENTORY headings intact — do NOT wrap it in a renamed block. The
            # translator sub-agent matches those literal headings and returns a
            # false INSUFFICIENT-DATA refusal if a wrapper hides them.
            out = msg["content"]
            if tools:
                schemas = "\n".join(
                    json.dumps({"name": t["function"]["name"],
                                "description": t["function"]["description"],
                                "parameters": t["function"]["parameters"]})
                    for t in tools)
                out += "\n\n## AVAILABLE CHECKS (JSON schemas)\n" + schemas
            return out
        if role == "user":
            return f"## USER QUESTION\n{msg['content']}"
        if role == "assistant":
            if msg.get("tool_calls"):
                tc = msg["tool_calls"][0]["function"]
                return ("(your previous tool call) "
                        + json.dumps({"tool": tc["name"],
                                       "args": json.loads(tc["arguments"] or "{}")}))
            return f"(your previous reply) {msg.get('content', '')}"
        if role == "tool":
            return f"TOOL RESULT:\n{msg['content']}"
        return str(msg.get("content", ""))

    def _session_key(self, messages: list[dict]) -> tuple:
        head = messages[0]["content"] if messages else ""
        return (id(messages), hash(head))

    # -- main entry ----------------------------------------------------------
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             role: str | None = None, format_hint: str | None = None) -> LLMReply:
        key = self._session_key(messages)
        conv_id, n_sent = self._sessions.get(key, (None, 0))
        if n_sent > len(messages):          # stale identity reuse — start over
            conv_id, n_sent = None, 0

        delta = messages[n_sent:]
        text = "\n\n".join(self._render(m, tools if i + n_sent == 0 else None)
                           for i, m in enumerate(delta)) or "(continue)"
        # tag only the FIRST message of a conversation; the worker routes on it
        # and remembers the role for the rest of the conversation.
        if role and n_sent == 0:
            text = f"ROLE: {role}\n\n{text}"
        # minimal per-message output-format reminder — this platform's models
        # need the format restated in the message, not just the system prompt.
        if format_hint:
            text = f"{text}\n\n## RESPOND WITH (format)\n{format_hint}"

        data = self._post(text, conv_id)
        conv_id = data.get("conversationId") or conv_id
        # +1 skips the assistant message the orchestrator will append for this
        # reply — the server already has it in its own state.
        self._sessions[key] = (conv_id, len(messages) + 1)

        reply = str(data.get("response") or "")
        if not tools:
            return LLMReply(content=reply)
        return self._parse_tool_reply(reply, key, conv_id, len(messages) + 1)

    def _parse_tool_reply(self, reply: str, key, conv_id, n_sent,
                          attempt: int = 0) -> LLMReply:
        text = reply.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.S).strip()
        m = _JSON_RE.search(text)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict) and "tool" in obj:
                    args = obj.get("args") or obj.get("arguments") or {}
                    if not isinstance(args, dict):
                        raise ValueError("args is not an object")
                    return LLMReply(content="", tool_calls=[
                        ToolCall(f"call_{uuid.uuid4().hex[:8]}",
                                 str(obj["tool"]), args)])
            except (json.JSONDecodeError, ValueError):
                if attempt < 2:
                    data = self._post(
                        "Your previous reply was not a single valid JSON tool "
                        "call. Reply again with EXACTLY one JSON object "
                        '{"tool": ..., "args": {...}} and nothing else — or '
                        "plain text (no JSON) if you are finished.", conv_id)
                    self._sessions[key] = (conv_id, n_sent)
                    return self._parse_tool_reply(
                        str(data.get("response") or ""), key, conv_id,
                        n_sent, attempt + 1)
                raise ProviderError("commotion: unparseable tool reply after "
                                    f"retries: {reply[:200]!r}")
        # no JSON tool call — the model is done; treat as final text
        return LLMReply(content=reply)


# ---------------------------------------------------------------------------
# Primary with fallback — sticky within one provider object (one turn)
# ---------------------------------------------------------------------------
class FallbackProvider:
    def __init__(self, primary, fallback, on_switch=None):
        self._chain = [primary, fallback]
        self._active = 0
        self._on_switch = on_switch or (lambda _msg: None)

    @property
    def name(self):
        return self._chain[self._active].name

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             role: str | None = None, format_hint: str | None = None) -> LLMReply:
        while True:
            try:
                return self._chain[self._active].chat(messages, tools, role,
                                                      format_hint)
            except ProviderError as e:
                if self._active + 1 >= len(self._chain):
                    raise
                self._active += 1
                self._on_switch(
                    f"{self._chain[self._active - 1].name} failed "
                    f"({str(e)[:120]}) — falling back to "
                    f"{self._chain[self._active].name}")
                # loop: the fallback gets the SAME full transcript (kept
                # locally in the neutral dialect), so nothing is lost


# ---------------------------------------------------------------------------
# Factory — reads env, builds the configured chain (fresh per scenario turn)
# ---------------------------------------------------------------------------
def build_provider(on_switch=None):
    """NETGUARD_LLM_PROVIDER: a single provider name (default 'commotion'), or a
    'primary,fallback' chain if you explicitly want a fallback. Default is
    Commotion only — the worker owns the persona, so the app sends thin
    (ROLE + data) messages that assume a persona-configured worker.

    NOTE: the thin-message mode is Commotion-specific. If you set 'ollama'
    here, the app would send it no behavioral instructions and it would fail —
    Ollama would need the full prompts restored. Kept only as an escape hatch.
    """
    spec = os.environ.get("NETGUARD_LLM_PROVIDER", "").strip().lower()
    if not spec:
        spec = "commotion" if os.environ.get("COMMOTION_API_KEY") else "ollama"

    def make(name: str):
        if name == "commotion":
            return CommotionProvider(
                url=os.environ.get("COMMOTION_URL", ""),
                api_key=os.environ.get("COMMOTION_API_KEY", ""),
                worker_id=os.environ.get("COMMOTION_WORKER_ID", ""),
                audience_id=os.environ.get("COMMOTION_AUDIENCE_ID", "RPA"),
                route_selector=os.environ.get("COMMOTION_ROUTE_SELECTOR",
                                               "aicoe_workspace"))
        if name == "ollama":
            return OpenAIProvider(
                base_url=os.environ.get("OLLAMA_BASE_URL", "https://ollama.com/v1"),
                api_key=os.environ.get("OLLAMA_API_KEY", ""),
                translator_model=os.environ.get("NETGUARD_TRANSLATOR_MODEL",
                                                 "qwen3-coder:480b"),
                synthesizer_model=os.environ.get("NETGUARD_SYNTHESIZER_MODEL",
                                                  "gpt-oss:120b"))
        raise ProviderError(f"unknown LLM provider {name!r}")

    names = [n.strip() for n in spec.split(",") if n.strip()]
    providers = [make(n) for n in names]
    if len(providers) == 1:
        return providers[0]
    return FallbackProvider(providers[0], providers[1], on_switch=on_switch)
