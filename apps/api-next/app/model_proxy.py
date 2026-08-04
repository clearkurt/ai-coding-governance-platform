import asyncio
import json
from collections.abc import AsyncIterator

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse

from app.dependencies import get_store
from app.settings import get_settings
from app.store import Store

ALLOWED_MODEL = "deepseek-v4-flash"
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "authorization",
    "content-length",
}
_semaphore: asyncio.Semaphore | None = None


async def get_model_http_client() -> AsyncIterator[httpx.AsyncClient]:
    settings = get_settings()
    async with httpx.AsyncClient(
        base_url=settings.deepseek_base_url, timeout=httpx.Timeout(settings.responses_timeout_seconds)
    ) as client:
        yield client


def _usage(payload: dict) -> tuple[str | None, int, int]:
    response = payload.get("response", payload)
    usage = response.get("usage", {}) if isinstance(response, dict) else {}
    return (
        response.get("id") if isinstance(response, dict) else None,
        int(usage.get("input_tokens", 0) or 0),
        int(usage.get("output_tokens", 0) or 0),
    )


async def proxy_responses(
    request: Request, store: Store = Depends(get_store), client: httpx.AsyncClient = Depends(get_model_http_client)
):
    settings = get_settings()
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid Content-Length") from error
        if declared_length < 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid Content-Length")
        if declared_length > settings.responses_max_body_bytes:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "request too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > settings.responses_max_body_bytes:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "request too large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid JSON") from error
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "JSON body must be an object")
    if body.get("model") != ALLOWED_MODEL:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "model is not allowed")
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "model token required")
    auth = await store.validate_model_token(authorization[7:], ALLOWED_MODEL)
    if not auth:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "model token expired, revoked, or task-bound validation failed"
        )
    if await store.model_usage_total(auth.team_id) >= settings.model_daily_token_quota_per_team:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "team model quota exhausted")
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.responses_max_concurrency)
    await _semaphore.acquire()
    try:
        if not settings.deepseek_api_key:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "model provider is not configured")
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS and k.lower().startswith("x-")
        }
        headers["authorization"] = f"Bearer {settings.deepseek_api_key}"
        headers["content-type"] = "application/json"
        upstream_request = client.build_request("POST", "/v1/responses", content=raw, headers=headers)
        # Always keep the upstream body raw. This preserves compressed payloads
        # when forwarding Content-Encoding instead of decoding and then
        # incorrectly retaining the original encoding header.
        upstream = await client.send(upstream_request, stream=True)
    except httpx.TimeoutException as error:
        _semaphore.release()
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "model provider timed out") from error
    except httpx.HTTPError as error:
        _semaphore.release()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "model provider unavailable") from error
    except Exception:
        _semaphore.release()
        raise
    response_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in HOP_HEADERS
        and k.lower() in {"content-type", "content-encoding", "x-request-id", "request-id"}
    }
    if not body.get("stream"):
        try:
            content = b"".join([chunk async for chunk in upstream.aiter_raw()])
        except httpx.TimeoutException as error:
            raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "model provider timed out") from error
        except httpx.HTTPError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "model provider unavailable") from error
        finally:
            await upstream.aclose()
            _semaphore.release()
        try:
            provider_id, input_tokens, output_tokens = _usage(json.loads(content))
            if provider_id:
                await store.record_model_usage(auth, provider_id, input_tokens, output_tokens)
        except (ValueError, TypeError):
            pass
        return Response(content, status_code=upstream.status_code, headers=response_headers)

    async def stream() -> AsyncIterator[bytes]:
        buffer = b""
        usage_record: tuple[str, int, int] | None = None
        try:
            async for chunk in upstream.aiter_raw():
                if await request.is_disconnected():
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.startswith(b"data: ") or line[6:] == b"[DONE]":
                        continue
                    try:
                        event = json.loads(line[6:])
                    except (ValueError, TypeError):
                        continue
                    if event.get("type") != "response.completed":
                        continue
                    provider_id, input_tokens, output_tokens = _usage(event)
                    if provider_id:
                        usage_record = (provider_id, input_tokens, output_tokens)
                yield chunk
        finally:
            if buffer.startswith(b"data: ") and buffer[6:] != b"[DONE]":
                try:
                    event = json.loads(buffer[6:])
                    if event.get("type") == "response.completed":
                        provider_id, input_tokens, output_tokens = _usage(event)
                        if provider_id:
                            usage_record = (provider_id, input_tokens, output_tokens)
                except (ValueError, TypeError):
                    pass
            await upstream.aclose()
            _semaphore.release()
            if usage_record:
                await store.record_model_usage(auth, *usage_record)

    return StreamingResponse(stream(), status_code=upstream.status_code, headers=response_headers)
