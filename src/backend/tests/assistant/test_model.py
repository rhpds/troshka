import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from app.services.assistant.model import ModelClient


def test_gateway_counts_tokens_before_inference_and_uses_only_configured_origin():
    seen = []
    original = httpx.AsyncClient

    def respond(request):
        seen.append(request)
        body = (
            {"input_tokens": 100}
            if request.url.path.endswith("count_tokens")
            else {
                "content": [{"type": "text", "text": "fixture"}],
                "usage": {"output_tokens": 10},
            }
        )
        return httpx.Response(200, json=body)

    cfg = {
        "base_url": "https://maas.example.test",
        "model": "configured-model",
        "api_key": "SYNTHETIC",
    }
    with (
        patch("app.services.assistant.model.settings", return_value=cfg),
        patch(
            "httpx.AsyncClient",
            side_effect=lambda **kw: original(
                transport=httpx.MockTransport(respond), **kw
            ),
        ),
    ):
        result = asyncio.run(
            ModelClient().complete([{"role": "user", "content": "help"}], [])
        )
    assert result["content"][0]["text"] == "fixture"
    assert [r.url.path for r in seen] == ["/v1/messages/count_tokens", "/v1/messages"]
    assert all(r.url.host == "maas.example.test" for r in seen)
    assert json.loads(seen[-1].content)["max_tokens"] == 2000


def test_token_limit_prevents_inference():
    original = httpx.AsyncClient
    paths = []

    def respond(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"input_tokens": 16001})

    with (
        patch(
            "app.services.assistant.model.settings",
            return_value={
                "base_url": "https://maas.example.test",
                "model": "fixture",
                "api_key": "SYNTHETIC",
            },
        ),
        patch(
            "httpx.AsyncClient",
            side_effect=lambda **kw: original(
                transport=httpx.MockTransport(respond), **kw
            ),
        ),
    ):
        with pytest.raises(ValueError):
            asyncio.run(
                ModelClient().complete([{"role": "user", "content": "help"}], [])
            )
    assert paths == ["/v1/messages/count_tokens"]
