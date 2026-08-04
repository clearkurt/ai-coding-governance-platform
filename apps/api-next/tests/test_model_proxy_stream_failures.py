import asyncio
import os
import uuid

os.environ.setdefault("COMPANY_AGENT_DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("COMPANY_AGENT_DEEPSEEK_API_KEY", "server-only-deepseek-secret")

import httpx
import pytest
from starlette.requests import Request

from app import model_proxy
from app.store import ModelAuthorization


class FakeStore:
    def __init__(self) -> None:
        self.auth = ModelAuthorization(
            uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "deepseek-v4-flash"
        )
        self.usage: list[tuple[str, int, int]] = []

    async def validate_model_token(self, raw_token, model):
        return self.auth

    async def model_usage_total(self, team_id):
        return 0

    async def record_model_usage(self, auth, provider_request_id, input_tokens, output_tokens):
        self.usage.append((provider_request_id, input_tokens, output_tokens))
        return True


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self) -> None:
        self.closed = True


class BlockingStream(TrackingStream):
    def __init__(self) -> None:
        super().__init__([])
        self.block = asyncio.Event()

    async def __aiter__(self):
        yield b'data: {"type":"response.output_text.delta"}\n'
        await self.block.wait()


def request_for_stream(disconnected: bool = False) -> Request:
    body = b'{"model":"deepseek-v4-flash","stream":true,"input":"x"}'
    first = True

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": body, "more_body": False}
        if disconnected:
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "headers": [(b"authorization", b"Bearer token"), (b"content-type", b"application/json")],
        },
        receive,
    )


async def response_for(stream: TrackingStream, request: Request, store: FakeStore):
    async def handler(_request: httpx.Request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://deepseek.test")
    response = await model_proxy.proxy_responses(request, store, client)
    return response, client


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.TimeoutException("late timeout"), httpx.ReadError("late read error")])
async def test_stream_error_after_first_chunk_closes_and_releases_without_usage(error) -> None:
    model_proxy._semaphore = asyncio.Semaphore(1)
    stream = TrackingStream([b'data: {"type":"response.output_text.delta"}\n'], error)
    store = FakeStore()
    response, client = await response_for(stream, request_for_stream(), store)
    iterator = response.body_iterator
    assert await anext(iterator)
    with pytest.raises(type(error)):
        await anext(iterator)
    assert stream.closed and model_proxy._semaphore._value == 1
    assert store.usage == []
    await response.background()
    assert model_proxy._semaphore._value == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_disconnect_before_first_yield_and_unstarted_response_cleanup_once() -> None:
    for disconnected in (True, False):
        model_proxy._semaphore = asyncio.Semaphore(1)
        stream = TrackingStream([b"data: partial\n"])
        store = FakeStore()
        response, client = await response_for(stream, request_for_stream(disconnected), store)
        if disconnected:
            assert [chunk async for chunk in response.body_iterator] == []
        else:
            await response.background()
        assert stream.closed and model_proxy._semaphore._value == 1
        await response.background()
        assert model_proxy._semaphore._value == 1 and store.usage == []
        await client.aclose()


@pytest.mark.asyncio
async def test_completed_usage_is_recorded_once_even_if_background_runs_again() -> None:
    model_proxy._semaphore = asyncio.Semaphore(1)
    payload = b'data: {"type":"response.completed","response":{"id":"complete","usage":{"input_tokens":2,"output_tokens":3}}}\n'
    stream, store = TrackingStream([payload]), FakeStore()
    response, client = await response_for(stream, request_for_stream(), store)
    assert b"".join([chunk async for chunk in response.body_iterator]) == payload
    await response.background()
    assert stream.closed and model_proxy._semaphore._value == 1
    assert store.usage == [("complete", 2, 3)]
    await client.aclose()


@pytest.mark.asyncio
async def test_cancelled_partial_consumer_closes_stream_and_does_not_deadlock_next_request() -> None:
    model_proxy._semaphore = asyncio.Semaphore(1)
    stream, store = BlockingStream(), FakeStore()
    response, client = await response_for(stream, request_for_stream(), store)
    assert await anext(response.body_iterator)
    pending = asyncio.create_task(anext(response.body_iterator))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert stream.closed and store.usage == [] and model_proxy._semaphore._value == 1
    await asyncio.wait_for(model_proxy._semaphore.acquire(), timeout=0.1)
    assert model_proxy._semaphore._value == 0
    model_proxy._semaphore.release()
    await response.background()
    assert model_proxy._semaphore._value == 1
    await client.aclose()
