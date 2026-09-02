"""LLM providers for the in-server troubleshooting agent.

Three providers are supported, on three wire formats:

* OpenAI on the **Responses** API, where a turn is a list of items and the
  model's own reasoning is one of them;
* Grok (xAI) on **Chat Completions**, which it serves OpenAI-compatibly;
* Claude on the **Messages** API, where a tool call is a content block.

All three take a reasoning effort, under three different names, and all three
consume the same neutral transcript and return a :class:`Reply` - so the agent
loop never learns which provider answered it. Keys are read from the
environment of the server process and never leave it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Anthropic requires an output budget, and adaptive thinking draws from the
#: same pool: reasoning over a few nodes' config can eat 8k on its own and
#: leave no room for the answer, which reaches the user as silence.
ANTHROPIC_MAX_TOKENS = 32_000

#: Wire formats an OpenAI-compatible provider can be pointed at.
SWAPPABLE_APIS = frozenset({"responses", "chat"})


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation, with its arguments still as the model wrote them."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Reply:
    """What a provider answered in one round."""

    text: str = ""
    tool_calls: Tuple[ToolCall, ...] = ()
    #: The assistant turn as the provider returned it. Claude has to be given
    #: its own blocks back verbatim on the next round, because a ``tool_use``
    #: block must be replayed together with the thinking block it came with.
    raw: Any = None
    #: Why the provider stopped, in its own words ("max_tokens",
    #: "max_output_tokens", "length", ...). Empty when it ended normally.
    stop: str = ""

    @property
    def truncated(self) -> bool:
        """Whether the answer was cut off by an output budget.

        A reasoning model can spend the whole budget thinking and return no
        text at all, which otherwise looks like a silent agent.
        """
        return self.stop in ("max_tokens", "max_output_tokens", "length")


@dataclass(frozen=True)
class ProviderSpec:
    """How to reach one provider and what to call it in the UI."""

    name: str
    label: str
    #: Wire format: ``responses``, ``chat`` (Completions) or ``anthropic``.
    default_api: str
    key_envs: Tuple[str, ...]
    model_envs: Tuple[str, ...]
    base_url_envs: Tuple[str, ...]
    default_model: str
    #: Reasoning effort levels the provider accepts, cheapest first. Empty when
    #: the provider has no such knob.
    efforts: Tuple[str, ...] = ()
    effort_envs: Tuple[str, ...] = ()
    #: Lets an OpenAI-compatible endpoint be moved between wire formats.
    api_envs: Tuple[str, ...] = ()
    default_base_url: Optional[str] = None
    #: The pip package the client comes from, named in the error when missing.
    package: str = "openai"

    def api_key(self) -> Optional[str]:
        for env in self.key_envs:
            value = os.environ.get(env)
            if value:
                return value
        return None

    def model(self) -> str:
        for env in self.model_envs:
            value = os.environ.get(env)
            if value:
                return value
        return self.default_model

    def base_url(self) -> Optional[str]:
        for env in self.base_url_envs:
            value = os.environ.get(env)
            if value:
                return value
        return self.default_base_url

    def api(self) -> str:
        for env in self.api_envs:
            value = (os.environ.get(env) or "").strip().lower()
            if value in SWAPPABLE_APIS:
                return value
        return self.default_api

    def effort(self) -> Optional[str]:
        """The configured effort, or None to let the model use its default."""
        for env in self.effort_envs:
            value = (os.environ.get(env) or "").strip().lower()
            if value:
                return value
        return None

    def configured(self) -> bool:
        return bool(self.api_key())


PROVIDERS: Dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        name="openai",
        label="OpenAI",
        default_api="responses",
        key_envs=("OPENAI_API_KEY",),
        model_envs=("OPENAI_MODEL",),
        base_url_envs=("OPENAI_BASE_URL",),
        default_model="gpt-5.6-sol",
        efforts=("none", "low", "medium", "high", "xhigh", "max"),
        effort_envs=("OPENAI_REASONING_EFFORT",),
        # Proxies in front of OpenAI often speak only Chat Completions.
        api_envs=("OPENAI_API",),
    ),
    "claude": ProviderSpec(
        name="claude",
        label="Claude",
        default_api="anthropic",
        key_envs=("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
        model_envs=("ANTHROPIC_MODEL", "CLAUDE_MODEL"),
        base_url_envs=("ANTHROPIC_BASE_URL",),
        default_model="claude-sonnet-5",
        efforts=("low", "medium", "high", "xhigh", "max"),
        effort_envs=("ANTHROPIC_EFFORT", "CLAUDE_EFFORT"),
        package="anthropic",
    ),
    "grok": ProviderSpec(
        name="grok",
        label="Grok",
        # xAI serves Chat Completions OpenAI-compatibly, so the openai client
        # reaches it with nothing but a different base URL.
        default_api="chat",
        key_envs=("XAI_API_KEY", "GROK_API_KEY"),
        model_envs=("XAI_MODEL", "GROK_MODEL"),
        base_url_envs=("XAI_BASE_URL",),
        default_model="grok-4.6",
        efforts=("low", "medium", "high", "xhigh"),
        effort_envs=("XAI_REASONING_EFFORT", "GROK_REASONING_EFFORT"),
        api_envs=("XAI_API", "GROK_API"),
        default_base_url="https://api.x.ai/v1",
    ),
}

#: Preference order when the operator did not pick one.
PROVIDER_ORDER: Tuple[str, ...] = ("openai", "claude", "grok")

#: Every key that turns some provider on, for the message shown when none is set.
KEY_ENVS: Tuple[str, ...] = tuple(
    env for name in PROVIDER_ORDER for env in PROVIDERS[name].key_envs
)


def get_provider(name: str) -> ProviderSpec:
    """Look a provider up by name, or say which names exist."""
    spec = PROVIDERS.get(name)
    if spec is None:
        known = ", ".join(PROVIDER_ORDER)
        raise ValueError(f"unknown provider '{name}'; known providers: {known}")
    return spec


def check_effort(spec: ProviderSpec, effort: Optional[str]) -> Optional[str]:
    """Validate a reasoning effort against what *spec* accepts."""
    if not effort:
        return None
    value = str(effort).strip().lower()
    if value in ("", "auto", "default"):
        return None
    if not spec.efforts:
        raise ValueError(f"{spec.label} does not take a reasoning effort")
    if value not in spec.efforts:
        levels = ", ".join(spec.efforts)
        raise ValueError(
            f"unknown reasoning effort '{value}' for {spec.label}; use one of {levels}"
        )
    return value


def configured_providers() -> List[ProviderSpec]:
    """Providers that have a key in the environment, in preference order."""
    return [PROVIDERS[name] for name in PROVIDER_ORDER if PROVIDERS[name].configured()]


def default_provider() -> Optional[ProviderSpec]:
    """The provider a chat uses when the browser does not name one.

    ``FCLI_LLM_PROVIDER`` pins it; otherwise the first one with a key wins.
    """
    wanted = (os.environ.get("FCLI_LLM_PROVIDER") or "").strip().lower()
    if wanted:
        spec = PROVIDERS.get(wanted)
        if spec is not None and spec.configured():
            return spec
    available = configured_providers()
    return available[0] if available else None


def build_client(spec: ProviderSpec) -> Any:
    """Construct the SDK client for *spec*."""
    key = spec.api_key()
    if not key:
        raise RuntimeError(
            f"{spec.label} is not configured; set {spec.key_envs[0]} "
            "on the fcli server process"
        )
    base_url = spec.base_url()
    if spec.api == "anthropic":
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover - only without the dep
            raise RuntimeError(
                f"the {spec.package} package is required for {spec.label}; "
                f"pip install {spec.package}"
            ) from exc
        kwargs: Dict[str, Any] = {"api_key": key}
        if base_url:
            kwargs["base_url"] = base_url
        return Anthropic(**kwargs)
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - only without the dep
        raise RuntimeError(
            f"the {spec.package} package is required for {spec.label}; "
            f"pip install {spec.package}"
        ) from exc
    kwargs = {"api_key": key}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


# --------------------------------------------------------------------------- #
# transcript
# --------------------------------------------------------------------------- #


@dataclass
class Transcript:
    """The turns of one chat, in a shape neither provider dictates."""

    entries: List[Dict[str, Any]] = field(default_factory=list)

    def user(self, content: str) -> None:
        self.entries.append({"role": "user", "content": content})

    def assistant(self, reply: Reply) -> None:
        self.entries.append(
            {
                "role": "assistant",
                "content": reply.text,
                "tool_calls": tuple(reply.tool_calls),
                "raw": reply.raw,
            }
        )

    def tool_result(self, call: ToolCall, content: str) -> None:
        self.entries.append(
            {"role": "tool", "id": call.id, "name": call.name, "content": content}
        )


# --------------------------------------------------------------------------- #
# tool schemas
# --------------------------------------------------------------------------- #


def openai_tool_schemas(tools: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        for tool in tools
    ]


def responses_tool_schemas(tools: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["parameters"],
            # Strict mode would make every filter argument mandatory; these are
            # deliberately optional, so opt out rather than be normalized in.
            "strict": False,
        }
        for tool in tools
    ]


def anthropic_tool_schemas(tools: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": tool["parameters"],
        }
        for tool in tools
    ]


# --------------------------------------------------------------------------- #
# completion
# --------------------------------------------------------------------------- #


def complete(
    spec: ProviderSpec,
    client: Any,
    model: str,
    system: str,
    transcript: Transcript,
    tools: Sequence[Dict[str, Any]],
    effort: Optional[str] = None,
) -> Reply:
    """One round against *spec*, normalized to a :class:`Reply`."""
    api = spec.api()
    if api == "anthropic":
        return _complete_anthropic(client, model, system, transcript, tools, effort)
    if api == "responses":
        return _complete_responses(client, model, system, transcript, tools, effort)
    return _complete_chat(client, model, system, transcript, tools, effort)


# ---- OpenAI (Responses) ---------------------------------------------------- #


def responses_input(transcript: Transcript) -> List[Any]:
    """The transcript as Responses items.

    A turn is a flat list of items rather than messages, so the assistant's own
    output items - reasoning included - go back in untouched and a tool result
    is an item that points at a ``call_id``.
    """
    items: List[Any] = []
    for entry in transcript.entries:
        role = entry["role"]
        if role == "user":
            items.append({"role": "user", "content": entry["content"]})
        elif role == "assistant":
            raw = entry.get("raw")
            if raw:
                items.extend(raw)
            elif entry.get("content"):
                items.append({"role": "assistant", "content": entry["content"]})
        elif role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": entry["id"],
                    "output": entry["content"],
                }
            )
    return items


def _complete_responses(
    client: Any,
    model: str,
    system: str,
    transcript: Transcript,
    tools: Sequence[Dict[str, Any]],
    effort: Optional[str],
) -> Reply:
    kwargs: Dict[str, Any] = {
        "model": model,
        "instructions": system,
        "input": responses_input(transcript),
        # Stateless: fabric data is not left in OpenAI's response store, and
        # reasoning items come back with encrypted_content so the loop can
        # replay them itself.
        "store": False,
    }
    if tools:
        kwargs["tools"] = responses_tool_schemas(tools)
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    return _from_responses(client.responses.create(**kwargs))


def _from_responses(response: Any) -> Reply:
    output = list(_block_field(response, "output") or [])
    text_parts: List[str] = []
    calls: List[ToolCall] = []
    for item in output:
        kind = str(_block_field(item, "type") or "")
        if kind == "message":
            for block in _block_field(item, "content") or []:
                if str(_block_field(block, "type") or "") in ("output_text", "text"):
                    text_parts.append(str(_block_field(block, "text") or ""))
        elif kind == "function_call":
            calls.append(
                ToolCall(
                    id=str(
                        _block_field(item, "call_id")
                        or _block_field(item, "id")
                        or ""
                    ),
                    name=str(_block_field(item, "name") or ""),
                    arguments=str(_block_field(item, "arguments") or ""),
                )
            )
    return Reply(
        text="".join(text_parts),
        tool_calls=tuple(calls),
        raw=output,
        stop=_responses_stop(response),
    )


def _responses_stop(response: Any) -> str:
    """The reason a Responses API call ended, if it was not a clean finish."""
    details = _block_field(response, "incomplete_details")
    reason = str(_block_field(details, "reason") or "") if details else ""
    if reason:
        return reason
    status = str(_block_field(response, "status") or "")
    return status if status and status != "completed" else ""


# ---- Grok (Chat Completions) ----------------------------------------------- #


def openai_messages(system: str, transcript: Transcript) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]
    for entry in transcript.entries:
        role = entry["role"]
        if role == "user":
            messages.append({"role": "user", "content": entry["content"]})
        elif role == "assistant":
            message: Dict[str, Any] = {
                "role": "assistant",
                "content": entry.get("content") or None,
            }
            calls = entry.get("tool_calls") or ()
            if calls:
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in calls
                ]
            messages.append(message)
        elif role == "tool":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": entry["id"],
                    "content": entry["content"],
                }
            )
    return messages


def _complete_chat(
    client: Any,
    model: str,
    system: str,
    transcript: Transcript,
    tools: Sequence[Dict[str, Any]],
    effort: Optional[str],
) -> Reply:
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": openai_messages(system, transcript),
        "stream": True,
    }
    if tools:
        kwargs["tools"] = openai_tool_schemas(tools)
        kwargs["tool_choice"] = "auto"
    if effort:
        kwargs["reasoning_effort"] = effort
    response = client.chat.completions.create(**kwargs)
    if _is_stream(response):
        return _from_openai_stream(response)
    return _from_openai_completion(response)


def _is_stream(response: Any) -> bool:
    if hasattr(response, "choices"):
        return False
    return hasattr(response, "__iter__")


def _delta_content(delta: Any) -> str:
    if delta is None:
        return ""
    if isinstance(delta, dict):
        return str(delta.get("content") or "")
    return str(getattr(delta, "content", None) or "")


def _delta_tool_calls(delta: Any) -> List[Any]:
    if delta is None:
        return []
    if isinstance(delta, dict):
        return list(delta.get("tool_calls") or [])
    return list(getattr(delta, "tool_calls", None) or [])


def _choice_finish(choice: Any) -> str:
    if isinstance(choice, dict):
        return str(choice.get("finish_reason") or "")
    return str(getattr(choice, "finish_reason", None) or "")


def _from_openai_stream(response: Any) -> Reply:
    content_parts: List[str] = []
    calls: Dict[int, Dict[str, str]] = {}
    finish = ""
    for chunk in response:
        choice = _first_choice(chunk)
        if choice is None:
            continue
        finish = _choice_finish(choice) or finish
        delta = _choice_delta(choice)
        text = _delta_content(delta)
        if text:
            content_parts.append(text)
        for item in _delta_tool_calls(delta):
            index, piece = _tool_call_piece(item)
            slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if piece.get("id"):
                slot["id"] = piece["id"]
            if piece.get("name"):
                slot["name"] = piece["name"]
            if piece.get("arguments"):
                slot["arguments"] += piece["arguments"]
    ordered = [
        ToolCall(
            id=calls[i]["id"] or f"call_{calls[i]['name'] or 'tool'}",
            name=calls[i]["name"],
            arguments=calls[i]["arguments"],
        )
        for i in sorted(calls)
    ]
    return Reply(
        text="".join(content_parts),
        tool_calls=tuple(ordered),
        stop="" if finish in ("stop", "tool_calls") else finish,
    )


def _from_openai_completion(response: Any) -> Reply:
    choice = _first_choice(response)
    if choice is None:
        return Reply()
    message = _choice_message(choice)
    if isinstance(message, dict):
        content = str(message.get("content") or "")
        raw_calls = message.get("tool_calls") or []
    else:
        content = str(getattr(message, "content", None) or "")
        raw_calls = getattr(message, "tool_calls", None) or []
    calls: List[ToolCall] = []
    for item in raw_calls:
        _index, piece = _tool_call_piece(item)
        calls.append(
            ToolCall(
                id=piece.get("id") or f"call_{piece.get('name') or 'tool'}",
                name=piece.get("name") or "",
                arguments=piece.get("arguments") or "",
            )
        )
    finish = _choice_finish(choice)
    return Reply(
        text=content,
        tool_calls=tuple(calls),
        stop="" if finish in ("stop", "tool_calls") else finish,
    )


def _first_choice(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        choices = obj.get("choices") or []
        return choices[0] if choices else None
    choices = getattr(obj, "choices", None) or []
    return choices[0] if choices else None


def _choice_delta(choice: Any) -> Any:
    if isinstance(choice, dict):
        return choice.get("delta")
    return getattr(choice, "delta", None)


def _choice_message(choice: Any) -> Any:
    if isinstance(choice, dict):
        return choice.get("message")
    return getattr(choice, "message", None)


def _tool_call_piece(item: Any) -> Tuple[int, Dict[str, str]]:
    if isinstance(item, dict):
        index = int(item.get("index") or 0)
        fn = item.get("function") or {}
        if not isinstance(fn, dict):
            fn = {}
        return index, {
            "id": str(item.get("id") or ""),
            "name": str(fn.get("name") or ""),
            "arguments": str(fn.get("arguments") or ""),
        }
    fn = getattr(item, "function", None)
    index = int(getattr(item, "index", 0) or 0)
    name = arguments = ""
    if isinstance(fn, dict):
        name = str(fn.get("name") or "")
        arguments = str(fn.get("arguments") or "")
    elif fn is not None:
        name = str(getattr(fn, "name", None) or "")
        arguments = str(getattr(fn, "arguments", None) or "")
    return index, {
        "id": str(getattr(item, "id", None) or ""),
        "name": name,
        "arguments": arguments,
    }


# ---- Claude ---------------------------------------------------------------- #


def anthropic_messages(transcript: Transcript) -> List[Dict[str, Any]]:
    """The transcript as Messages API turns.

    Tool results are blocks of a *user* turn here, and every result of one
    assistant turn has to arrive in the same turn - so consecutive results are
    gathered rather than sent one message each.
    """
    messages: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []

    def flush() -> None:
        if pending:
            messages.append({"role": "user", "content": list(pending)})
            pending.clear()

    for entry in transcript.entries:
        role = entry["role"]
        if role == "tool":
            pending.append(
                {
                    "type": "tool_result",
                    "tool_use_id": entry["id"],
                    "content": entry["content"],
                }
            )
            continue
        flush()
        if role == "user":
            messages.append({"role": "user", "content": entry["content"]})
            continue
        blocks = entry.get("raw")
        if not blocks:
            blocks = []
            if entry.get("content"):
                blocks.append({"type": "text", "text": entry["content"]})
            for call in entry.get("tool_calls") or ():
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": _json_object(call.arguments),
                    }
                )
        if not blocks:
            # An empty assistant turn is a 400 on the Messages API.
            continue
        messages.append({"role": "assistant", "content": blocks})
    flush()
    return messages


def _json_object(raw: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _complete_anthropic(
    client: Any,
    model: str,
    system: str,
    transcript: Transcript,
    tools: Sequence[Dict[str, Any]],
    effort: Optional[str],
) -> Reply:
    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": ANTHROPIC_MAX_TOKENS,
        "system": system,
        "messages": anthropic_messages(transcript),
    }
    if tools:
        kwargs["tools"] = anthropic_tool_schemas(tools)
    if effort:
        # Effort lives in output_config, not in the thinking object.
        kwargs["output_config"] = {"effort": effort}
    return _from_anthropic(client.messages.create(**kwargs))


def _block_field(block: Any, name: str) -> Any:
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _from_anthropic(response: Any) -> Reply:
    blocks = _block_field(response, "content") or []
    text_parts: List[str] = []
    calls: List[ToolCall] = []
    for block in blocks:
        kind = str(_block_field(block, "type") or "")
        if kind == "text":
            text_parts.append(str(_block_field(block, "text") or ""))
        elif kind == "tool_use":
            arguments = _block_field(block, "input")
            calls.append(
                ToolCall(
                    id=str(_block_field(block, "id") or ""),
                    name=str(_block_field(block, "name") or ""),
                    arguments=json.dumps(arguments or {}, default=str),
                )
            )
    # Thinking blocks are kept in raw and replayed untouched; Claude rejects a
    # tool_use that comes back without the thinking that produced it.
    stop = str(_block_field(response, "stop_reason") or "")
    return Reply(
        text="".join(text_parts),
        tool_calls=tuple(calls),
        raw=blocks,
        stop="" if stop in ("end_turn", "tool_use", "stop_sequence") else stop,
    )
