"""Tools are usable across turns, including fragmented cross-protocol streams."""

import json

import httpx
import pytest

pytestmark = pytest.mark.anyio

TOOLS = [
    {
        "type": "function",
        "name": "lookup",
        "description": "Look up a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]
CITIES = ("Paris", "Tokyo")


class Fragments(httpx.AsyncByteStream):
    def __init__(self, events):
        self.events = events

    async def __aiter__(self):
        for event in self.events:
            payload = (
                f"data: {json.dumps(event) if isinstance(event, dict) else event}\n\n".encode()
            )
            # Split even the wire framing: transport chunks are not protocol events.
            yield payload[:7]
            yield payload[7:]


def chat_reply(tool_turn, streaming):
    calls = [
        {
            "id": f"call_{index}",
            "type": "function",
            "function": {"name": "lookup", "arguments": json.dumps({"city": city})},
        }
        for index, city in enumerate(CITIES)
    ]
    usage = {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
    if not streaming:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-tools" if tool_turn else "chatcmpl-answer",
                "object": "chat.completion",
                "model": "model-a",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            **(
                                {"content": None, "tool_calls": calls}
                                if tool_turn
                                else {"content": "Paris: 20; Tokyo: 25"}
                            ),
                        },
                        "finish_reason": "tool_calls" if tool_turn else "stop",
                    }
                ],
                "usage": usage,
            },
        )
    events = []
    if tool_turn:
        for index, call in enumerate(calls):
            arguments = call["function"]["arguments"]
            events.append(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "id": call["id"],
                                        "type": "function",
                                        "function": {"name": "lookup", "arguments": arguments[:8]},
                                    }
                                ]
                            },
                        }
                    ]
                }
            )
            events.append(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": index, "function": {"arguments": arguments[8:]}}
                                ]
                            },
                        }
                    ]
                }
            )
    else:
        events.append({"choices": [{"index": 0, "delta": {"content": "Paris: 20; Tokyo: 25"}}]})
    events.extend(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls" if tool_turn else "stop",
                    }
                ],
                "usage": usage,
            },
            "[DONE]",
        ]
    )
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Fragments(events)
    )


def messages_reply(tool_turn, streaming):
    blocks = (
        [
            {"type": "tool_use", "id": f"call_{index}", "name": "lookup", "input": {"city": city}}
            for index, city in enumerate(CITIES)
        ]
        if tool_turn
        else [{"type": "text", "text": "Paris: 20; Tokyo: 25"}]
    )
    message = {
        "id": "msg_tools" if tool_turn else "msg_answer",
        "type": "message",
        "role": "assistant",
        "model": "model-a",
        "content": blocks,
        "stop_reason": "tool_use" if tool_turn else "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 8},
    }
    if not streaming:
        return httpx.Response(200, json=message)
    events = [
        {
            "type": "message_start",
            "message": {
                **message,
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": 12, "output_tokens": 0},
            },
        }
    ]
    for index, block in enumerate(blocks):
        events.append(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {**block, **({"input": {}} if tool_turn else {"text": ""})},
            }
        )
        if tool_turn:
            arguments = json.dumps(block["input"])
            for part in (arguments[:8], arguments[8:]):
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": part},
                    }
                )
        else:
            events.append(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": block["text"]},
                }
            )
        events.append({"type": "content_block_stop", "index": index})
    events.extend(
        [
            {
                "type": "message_delta",
                "delta": {"stop_reason": message["stop_reason"]},
                "usage": {"output_tokens": 8},
            },
            {"type": "message_stop"},
        ]
    )
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Fragments(events)
    )


def response_document(response, streaming):
    assert response.status_code == 200, response.text
    if not streaming:
        return response.json()
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")
    ]
    completed = [event["response"] for event in events if event.get("type") == "response.completed"]
    assert len(completed) == 1, events
    assert not any(event.get("type") in {"error", "response.failed"} for event in events)
    arguments = {}
    for event in events:
        if event.get("type") == "response.output_item.added":
            item = event["item"]
            if item["type"] == "function_call":
                arguments[item["id"]] = ""
        elif event.get("type") == "response.function_call_arguments.delta":
            assert event["item_id"] in arguments  # Announce a call before streaming arguments.
            arguments[event["item_id"]] += event["delta"]
    for item in completed[0]["output"]:
        if item["type"] == "function_call":
            assert json.loads(arguments[item["id"]]) == json.loads(item["arguments"])
    return completed[0]


@pytest.mark.parametrize("protocol", ["openai-chat", "anthropic"])
@pytest.mark.parametrize("streaming", [False, True], ids=["buffered", "streaming"])
async def test_tool_results_and_followup_survive_reopen(
    open_lite, register_channel, protocol, streaming
):
    sent = []

    def upstream(request):
        body = json.loads(request.content)
        sent.append(body)
        assert body["model"] == "model-a"
        assert request.url.path.endswith(
            "chat/completions" if protocol == "openai-chat" else "messages"
        )
        assert body["tools"]
        turn = len(sent)
        if turn > 1:
            messages = body["messages"]
            if protocol == "openai-chat":
                calls = [call for message in messages for call in message.get("tool_calls", [])]
                results = {
                    message["tool_call_id"]: message["content"]
                    for message in messages
                    if message["role"] == "tool"
                }
                assert {
                    call["id"]: json.loads(call["function"]["arguments"])["city"] for call in calls
                } == {"call_0": "Paris", "call_1": "Tokyo"}
            else:
                blocks = [
                    block
                    for message in messages
                    if isinstance(message["content"], list)
                    for block in message["content"]
                ]
                calls = [block for block in blocks if block["type"] == "tool_use"]
                results = {
                    block["tool_use_id"]: block["content"]
                    for block in blocks
                    if block["type"] == "tool_result"
                }
                assert {call["id"]: call["input"]["city"] for call in calls} == {
                    "call_0": "Paris",
                    "call_1": "Tokyo",
                }
            assert set(results) == {"call_0", "call_1"}
            assert "20" in json.dumps(results["call_0"])
            assert "25" in json.dumps(results["call_1"])
            if turn == 3:
                assert "Which is warmer?" in json.dumps(messages)
                assert "Paris: 20; Tokyo: 25" in json.dumps(messages)
        return (chat_reply if protocol == "openai-chat" else messages_reply)(
            turn == 1, body.get("stream", False)
        )

    async with open_lite(upstream) as app:
        await register_channel(app.client, protocol=protocol)
        first = response_document(
            await app.client.post(
                "/v1/responses",
                json={
                    "model": "demo/model-a",
                    "input": "Compare Paris and Tokyo temperatures",
                    "tools": TOOLS,
                    "stream": streaming,
                },
            ),
            streaming,
        )
        calls = [item for item in first["output"] if item["type"] == "function_call"]
        assert len(calls) == 2
        assert {item["call_id"]: json.loads(item["arguments"])["city"] for item in calls} == {
            "call_0": "Paris",
            "call_1": "Tokyo",
        }
        assert all(item["name"] == "lookup" for item in calls)

    # The client keeps only the response ID; Lite must reload translated history from disk.
    async with open_lite(upstream) as app:
        second = response_document(
            await app.client.post(
                "/v1/responses",
                json={
                    "model": "demo/model-a",
                    "previous_response_id": first["id"],
                    "input": [
                        {"type": "function_call_output", "call_id": "call_1", "output": "25"},
                        {"type": "function_call_output", "call_id": "call_0", "output": "20"},
                    ],
                    "tools": TOOLS,
                    "stream": streaming,
                },
            ),
            streaming,
        )
        assert second["status"] == "completed"
        assert "Paris: 20; Tokyo: 25" in json.dumps(second["output"])
        third = response_document(
            await app.client.post(
                "/v1/responses",
                json={
                    "model": "demo/model-a",
                    "previous_response_id": second["id"],
                    "input": "Which is warmer?",
                    "tools": TOOLS,
                    "stream": streaming,
                },
            ),
            streaming,
        )
        assert third["status"] == "completed"
        assert len(sent) == 3
        assert len(list(app.services.telemetry.store.rows())) == 3


async def test_unavailable_history_does_not_send_and_new_conversation_recovers(
    open_lite, register_channel
):
    sent = []

    def upstream(request):
        sent.append(json.loads(request.content))
        return chat_reply(False, False)

    async with open_lite(upstream) as app:
        await register_channel(app.client)
        failed = await app.client.post(
            "/v1/responses",
            json={
                "model": "demo/model-a",
                "previous_response_id": "resp_not_here",
                "input": "Continue",
            },
        )
        assert failed.status_code == 400, failed.text
        assert "previous response" in failed.text
        assert sent == []
        recovered = await app.client.post(
            "/v1/responses", json={"model": "demo/model-a", "input": "Start a new conversation"}
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["status"] == "completed"
        assert len(sent) == 1


@pytest.mark.parametrize("protocol", ["openai-chat", "openai-responses", "anthropic"])
async def test_native_client_can_return_tool_results(open_lite, register_channel, protocol):
    sent = []
    response_id = "resp_native_tools"

    def upstream(request):
        body = json.loads(request.content)
        sent.append(body)
        assert body["model"] == "model-a"
        if len(sent) == 2:
            if protocol == "openai-responses":
                assert body["previous_response_id"] == response_id
                results = {item["call_id"]: item["output"] for item in body["input"]}
            elif protocol == "openai-chat":
                results = {
                    item["tool_call_id"]: item["content"]
                    for item in body["messages"]
                    if item["role"] == "tool"
                }
                assert len(body["messages"][1]["tool_calls"]) == 2
            else:
                results = {
                    item["tool_use_id"]: item["content"] for item in body["messages"][-1]["content"]
                }
                assert len(body["messages"][1]["content"]) == 2
            assert results == {"call_0": "20", "call_1": "25"}
        if protocol == "openai-chat":
            return chat_reply(len(sent) == 1, False)
        if protocol == "anthropic":
            return messages_reply(len(sent) == 1, False)
        output = (
            [
                {
                    "type": "function_call",
                    "id": f"fc_{index}",
                    "call_id": f"call_{index}",
                    "name": "lookup",
                    "arguments": json.dumps({"city": city}),
                    "status": "completed",
                }
                for index, city in enumerate(CITIES)
            ]
            if len(sent) == 1
            else [
                {
                    "type": "message",
                    "id": "msg_answer",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "Paris: 20; Tokyo: 25", "annotations": []}
                    ],
                }
            ]
        )
        return httpx.Response(
            200,
            json={
                "id": response_id if len(sent) == 1 else "resp_native_answer",
                "object": "response",
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": 12, "output_tokens": 8},
            },
        )

    async with open_lite(upstream) as app:
        await register_channel(app.client, protocol=protocol)
        if protocol == "openai-responses":
            path = "/v1/responses"
            body = {"model": "demo/model-a", "input": "Compare two cities", "tools": TOOLS}
        else:
            path = "/v1/chat/completions" if protocol == "openai-chat" else "/v1/messages"
            tools = (
                [
                    {
                        "type": "function",
                        "function": {
                            key: value for key, value in TOOLS[0].items() if key != "type"
                        },
                    }
                ]
                if protocol == "openai-chat"
                else [
                    {
                        "name": "lookup",
                        "description": "Look up a city",
                        "input_schema": TOOLS[0]["parameters"],
                    }
                ]
            )
            body = {
                "model": "demo/model-a",
                "messages": [{"role": "user", "content": "Compare two cities"}],
                "tools": tools,
                "max_tokens": 128,
            }
        first_response = await app.client.post(path, json=body)
        assert first_response.status_code == 200, first_response.text
        first = first_response.json()
        if protocol == "openai-responses":
            calls = first["output"]
            assert {call["call_id"]: json.loads(call["arguments"])["city"] for call in calls} == {
                "call_0": "Paris",
                "call_1": "Tokyo",
            }
            followup = {
                **body,
                "previous_response_id": first["id"],
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": "20"
                        if json.loads(call["arguments"])["city"] == "Paris"
                        else "25",
                    }
                    for call in calls
                ],
            }
        elif protocol == "openai-chat":
            message = first["choices"][0]["message"]
            assert first["choices"][0]["finish_reason"] == "tool_calls"
            followup = {
                **body,
                "messages": [
                    *body["messages"],
                    message,
                    *[
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": "20"
                            if json.loads(call["function"]["arguments"])["city"] == "Paris"
                            else "25",
                        }
                        for call in message["tool_calls"]
                    ],
                ],
            }
        else:
            assert first["stop_reason"] == "tool_use"
            followup = {
                **body,
                "messages": [
                    *body["messages"],
                    {"role": "assistant", "content": first["content"]},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call["id"],
                                "content": "20" if call["input"]["city"] == "Paris" else "25",
                            }
                            for call in first["content"]
                        ],
                    },
                ],
            }
        second = await app.client.post(path, json=followup)
        assert second.status_code == 200, second.text
        assert "Paris: 20; Tokyo: 25" in second.text
        assert len(sent) == 2
        rows = list(app.services.telemetry.store.rows())
        assert len(rows) == 2
        assert all(row["status"] == 200 for row in rows)
