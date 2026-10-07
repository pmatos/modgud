"""Contract tests for validated model completions."""

import json

import httpx
from openai import OpenAI

from modgud.model_completions import request_parsed_completion
from modgud.models import RoutedModelClient


def _routed_client(
    outputs: list[str | None], requests: list[dict[str, object]]
) -> RoutedModelClient:
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        content = outputs.pop(0)
        return httpx.Response(
            200,
            json={
                "id": "completion",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": (
                    [{"index": 0, "message": {"role": "assistant", "content": content}}]
                    if content is not None
                    else []
                ),
            },
        )

    client = OpenAI(
        base_url="http://localhost/v1",
        api_key="test",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    return RoutedModelClient(client=client, model="test-model")


def test_json_completion_retries_a_missing_choice() -> None:
    requests: list[dict[str, object]] = []
    routed = _routed_client([None, '{"value": 2}'], requests)
    try:
        result = request_parsed_completion(
            routed,
            "source text",
            system_prompt="return a value",
            parse=lambda content: json.loads(content)["value"],
            json_object=True,
        )
    finally:
        routed.client.close()

    assert result == 2
    assert len(requests) == 2
    assert requests[0]["messages"] == [
        {"role": "system", "content": "return a value"},
        {"role": "user", "content": "source text"},
    ]
    assert requests[0]["response_format"] == {"type": "json_object"}


def test_plain_completion_retries_parser_rejection_then_returns_none() -> None:
    requests: list[dict[str, object]] = []
    routed = _routed_client(["  ", "  "], requests)

    def nonempty(content: str) -> str:
        stripped = content.strip()
        if not stripped:
            raise ValueError("empty output")
        return stripped

    try:
        result = request_parsed_completion(
            routed, "source text", system_prompt="summarize", parse=nonempty
        )
    finally:
        routed.client.close()

    assert result is None
    assert len(requests) == 2
    assert "response_format" not in requests[0]
