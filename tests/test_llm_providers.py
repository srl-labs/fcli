"""Provider selection and the two wire formats behind it."""

import json

import pytest

from nornir_srl.server import llm
from nornir_srl.server.llm import (
    KEY_ENVS,
    Reply,
    ToolCall,
    Transcript,
    anthropic_messages,
    anthropic_tool_schemas,
    check_effort,
    configured_providers,
    default_provider,
    get_provider,
    openai_messages,
    openai_tool_schemas,
    responses_input,
    responses_tool_schemas,
)

TOOLS = [
    {
        "name": "bgp_peers",
        "description": "BGP sessions.",
        "parameters": {"type": "object", "properties": {}},
    }
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    extra = (
        "FCLI_LLM_PROVIDER",
        "OPENAI_MODEL",
        "ANTHROPIC_MODEL",
        "OPENAI_API",
        "XAI_API",
        "GROK_API",
        "OPENAI_REASONING_EFFORT",
        "XAI_REASONING_EFFORT",
        "GROK_REASONING_EFFORT",
        "ANTHROPIC_EFFORT",
        "CLAUDE_EFFORT",
    )
    for env in KEY_ENVS + extra:
        monkeypatch.delenv(env, raising=False)


def test_no_provider_is_configured_without_keys():
    assert configured_providers() == []
    assert default_provider() is None


def test_keys_decide_which_providers_are_offered(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert [spec.name for spec in configured_providers()] == ["claude", "grok"]
    # Preference order picks claude, not the key that was set first.
    assert default_provider().name == "claude"


def test_fcli_llm_provider_pins_the_choice(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("XAI_API_KEY", "x")
    monkeypatch.setenv("FCLI_LLM_PROVIDER", "grok")
    assert default_provider().name == "grok"


def test_an_unconfigured_pin_falls_back_to_a_configured_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("FCLI_LLM_PROVIDER", "claude")
    assert default_provider().name == "openai"


def test_openai_talks_the_responses_api():
    spec = get_provider("openai")
    assert spec.api() == "responses"
    assert spec.model() == "gpt-5.6-sol"


def test_openai_can_be_moved_back_to_chat_completions(monkeypatch):
    # For proxies in front of OpenAI that never implemented Responses.
    monkeypatch.setenv("OPENAI_API", "chat")
    assert get_provider("openai").api() == "chat"
    monkeypatch.setenv("OPENAI_API", "nonsense")
    assert get_provider("openai").api() == "responses"


def test_grok_talks_chat_completions_at_xai(monkeypatch):
    monkeypatch.setenv("GROK_API_KEY", "x")
    spec = get_provider("grok")
    assert spec.api() == "chat"
    assert spec.base_url() == "https://api.x.ai/v1"
    assert spec.model() == "grok-4.6"
    monkeypatch.setenv("XAI_MODEL", "grok-4.5")
    assert spec.model() == "grok-4.5"


def test_claude_talks_the_messages_api():
    spec = get_provider("claude")
    assert spec.api() == "anthropic"
    assert spec.model() == "claude-sonnet-5"


def test_effort_comes_from_the_environment(monkeypatch):
    spec = get_provider("openai")
    assert spec.effort() is None
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    assert spec.effort() == "high"


def test_effort_is_checked_against_the_provider():
    openai = get_provider("openai")
    assert check_effort(openai, "xhigh") == "xhigh"
    assert check_effort(openai, None) is None
    # "auto" is how the drawer asks for the model's own default.
    assert check_effort(openai, "auto") is None
    with pytest.raises(ValueError) as excinfo:
        check_effort(get_provider("grok"), "max")
    assert "low, medium, high, xhigh" in str(excinfo.value)


def test_unknown_provider_names_the_known_ones():
    with pytest.raises(ValueError) as excinfo:
        get_provider("llama")
    assert "openai, claude, grok" in str(excinfo.value)


def test_building_a_client_without_a_key_says_which_one_to_set():
    with pytest.raises(RuntimeError) as excinfo:
        llm.build_client(get_provider("claude"))
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


def _tool_transcript() -> Transcript:
    transcript = Transcript()
    transcript.user("why is bgp down?")
    call = ToolCall(id="call_1", name="bgp_peers", arguments='{"inv_filter": "role=leaf"}')
    transcript.assistant(Reply(text="Checking.", tool_calls=(call,)))
    transcript.tool_result(call, '{"rows": []}')
    return transcript


def test_openai_transcript_keeps_tool_calls_on_the_assistant_turn():
    messages = openai_messages("be brief", _tool_transcript())
    assert messages[0] == {"role": "system", "content": "be brief"}
    assert messages[1]["role"] == "user"
    assistant = messages[2]
    assert assistant["tool_calls"][0]["function"]["name"] == "bgp_peers"
    assert messages[3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"rows": []}',
    }


def test_claude_transcript_turns_tool_calls_into_blocks():
    messages = anthropic_messages(_tool_transcript())
    assert messages[0] == {"role": "user", "content": "why is bgp down?"}
    blocks = messages[1]["content"]
    assert messages[1]["role"] == "assistant"
    assert [b["type"] for b in blocks] == ["text", "tool_use"]
    assert blocks[1]["input"] == {"inv_filter": "role=leaf"}
    result = messages[2]
    assert result["role"] == "user"
    assert result["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": '{"rows": []}',
        }
    ]


def test_claude_results_of_one_turn_travel_together():
    transcript = Transcript()
    transcript.user("check both")
    first = ToolCall(id="a", name="bgp_peers", arguments="{}")
    second = ToolCall(id="b", name="lldp_neighbors", arguments="{}")
    transcript.assistant(Reply(tool_calls=(first, second)))
    transcript.tool_result(first, "{}")
    transcript.tool_result(second, "{}")
    messages = anthropic_messages(transcript)
    # Claude requires every tool_result of one assistant turn in a single turn.
    assert len(messages) == 3
    assert [b["tool_use_id"] for b in messages[2]["content"]] == ["a", "b"]


def test_claude_assistant_blocks_are_replayed_verbatim():
    raw = [
        {"type": "thinking", "thinking": "...", "signature": "sig"},
        {"type": "tool_use", "id": "a", "name": "bgp_peers", "input": {}},
    ]
    transcript = Transcript()
    transcript.user("hi")
    transcript.assistant(
        Reply(tool_calls=(ToolCall("a", "bgp_peers", "{}"),), raw=raw)
    )
    assert anthropic_messages(transcript)[1]["content"] is raw


def test_responses_transcript_replays_items_and_answers_call_ids():
    transcript = Transcript()
    transcript.user("why is bgp down?")
    call = ToolCall(id="call_1", name="bgp_peers", arguments="{}")
    raw = [
        {"type": "reasoning", "encrypted_content": "opaque"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "bgp_peers",
            "arguments": "{}",
        },
    ]
    transcript.assistant(Reply(tool_calls=(call,), raw=raw))
    transcript.tool_result(call, '{"rows": []}')
    items = responses_input(transcript)
    assert items[0] == {"role": "user", "content": "why is bgp down?"}
    # Reasoning items go back untouched, so the model keeps its own train of thought.
    assert items[1:3] == raw
    assert items[3] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": '{"rows": []}',
    }


def test_responses_transcript_carries_plain_history():
    transcript = Transcript()
    transcript.user("hi")
    transcript.assistant(Reply(text="hello"))
    transcript.user("and now?")
    assert responses_input(transcript) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "and now?"},
    ]


def test_a_responses_round_is_normalized_like_any_other():
    output = [
        {"type": "reasoning", "encrypted_content": "opaque"},
        {"type": "message", "content": [{"type": "output_text", "text": "Checking."}]},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "bgp_peers",
            "arguments": '{"inv_filter": "role=leaf"}',
        },
    ]
    client = FakeResponsesClient(output)
    transcript = Transcript()
    transcript.user("bgp?")
    reply = llm.complete(
        get_provider("openai"),
        client,
        "gpt-5.6-sol",
        "sys",
        transcript,
        TOOLS,
        "high",
    )
    assert reply.text == "Checking."
    assert reply.tool_calls[0].id == "call_1"
    assert json.loads(reply.tool_calls[0].arguments) == {"inv_filter": "role=leaf"}
    assert reply.raw == output
    sent = client.responses.kwargs
    assert sent["instructions"] == "sys"
    assert sent["store"] is False
    assert sent["reasoning"] == {"effort": "high"}
    assert sent["tools"][0]["name"] == "bgp_peers"


def test_tool_schemas_are_rendered_per_provider():
    assert responses_tool_schemas(TOOLS) == [
        {
            "type": "function",
            "name": "bgp_peers",
            "description": "BGP sessions.",
            "parameters": {"type": "object", "properties": {}},
            "strict": False,
        }
    ]
    assert openai_tool_schemas(TOOLS) == [
        {
            "type": "function",
            "function": {
                "name": "bgp_peers",
                "description": "BGP sessions.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    assert anthropic_tool_schemas(TOOLS) == [
        {
            "name": "bgp_peers",
            "description": "BGP sessions.",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]


class FakeCalls:
    """Records the request and answers with a canned response."""

    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


class FakeAnthropic:
    def __init__(self, response):
        self.messages = FakeCalls(response)


class FakeResponsesClient:
    def __init__(self, output):
        self.responses = FakeCalls({"output": output})


def test_a_claude_round_is_normalized_like_any_other():
    response = {
        "content": [
            {"type": "text", "text": "Let me look."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "bgp_peers",
                "input": {"inv_filter": "role=leaf"},
            },
        ]
    }
    client = FakeAnthropic(response)
    transcript = Transcript()
    transcript.user("bgp?")
    reply = llm.complete(
        get_provider("claude"),
        client,
        "claude-sonnet-5",
        "sys",
        transcript,
        TOOLS,
        "medium",
    )
    assert reply.text == "Let me look."
    assert len(reply.tool_calls) == 1
    call = reply.tool_calls[0]
    assert (call.id, call.name) == ("toolu_1", "bgp_peers")
    assert json.loads(call.arguments) == {"inv_filter": "role=leaf"}
    assert reply.raw is response["content"]
    sent = client.messages.kwargs
    assert sent["system"] == "sys"
    assert sent["tools"][0]["input_schema"] == TOOLS[0]["parameters"]
    assert sent["max_tokens"] == llm.ANTHROPIC_MAX_TOKENS
    # Claude takes effort in output_config, not inside the thinking object.
    assert sent["output_config"] == {"effort": "medium"}


def test_claude_reports_running_out_of_output_budget():
    # Thinking can eat the whole budget and leave no text block behind.
    client = FakeAnthropic({"content": [], "stop_reason": "max_tokens"})
    transcript = Transcript()
    transcript.user("explain the fabric")
    reply = llm.complete(
        get_provider("claude"), client, "claude-sonnet-5", "sys", transcript, TOOLS, None
    )
    assert reply.text == ""
    assert reply.stop == "max_tokens"
    assert reply.truncated


def test_a_normal_claude_stop_is_not_a_stop_reason():
    client = FakeAnthropic(
        {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}
    )
    transcript = Transcript()
    transcript.user("hi")
    reply = llm.complete(
        get_provider("claude"), client, "claude-sonnet-5", "sys", transcript, TOOLS, None
    )
    assert reply.stop == ""
    assert not reply.truncated


def test_the_responses_api_reports_an_incomplete_answer():
    reply = llm._from_responses(
        {
            "output": [],
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
        }
    )
    assert reply.stop == "max_output_tokens"
    assert reply.truncated
    clean = llm._from_responses(
        {"output": [{"type": "message", "content": [{"type": "text", "text": "hi"}]}],
         "status": "completed"}
    )
    assert (clean.text, clean.stop, clean.truncated) == ("hi", "", False)


def test_chat_completions_reports_a_length_cut_off():
    chunks = [{"choices": [{"delta": {"content": "part"}, "finish_reason": "length"}]}]
    reply = llm._from_openai_stream(chunks)
    assert (reply.text, reply.stop) == ("part", "length")
    assert reply.truncated
    ok = [{"choices": [{"delta": {"content": "all"}, "finish_reason": "stop"}]}]
    assert llm._from_openai_stream(ok).stop == ""


def test_a_round_without_tools_omits_the_tool_field():
    # The answer-anyway round must not offer tools the model could ask for.
    client = FakeAnthropic({"content": [{"type": "text", "text": "ok"}]})
    transcript = Transcript()
    transcript.user("hi")
    llm.complete(
        get_provider("claude"), client, "claude-sonnet-5", "sys", transcript, [], None
    )
    assert "tools" not in client.messages.kwargs
