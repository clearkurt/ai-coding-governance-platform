import json
import os
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

app = FastAPI()


@app.post("/v1/responses")
async def responses(request: Request) -> Response:
    expected_key = os.environ["TEST_UPSTREAM_API_KEY"]
    counter = Path(os.environ["TEST_UPSTREAM_COUNTER"])
    body = await request.json()
    if request.headers.get("authorization") != f"Bearer {expected_key}":
        return Response(status_code=401)
    if body.get("model") != "deepseek-v4-flash" or body.get("stream") is not True:
        return Response(status_code=400)
    counter.write_text(str(int(counter.read_text(encoding="ascii")) + 1), encoding="ascii")
    completed = {
        "type": "response.completed",
        "response": {
            "id": "resp_tls_network",
            "object": "response",
            "status": "completed",
            "model": "deepseek-v4-flash",
            "output": [],
            "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        },
    }
    events = [
        {"type": "response.created", "response": {"id": "resp_tls_network", "status": "in_progress"}},
        {"type": "response.in_progress", "response": {"id": "resp_tls_network", "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message"}},
        {
            "type": "response.content_part.added",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        },
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
        {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": "ok"},
        {
            "type": "response.content_part.done",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "ok"},
        },
        {"type": "response.output_item.done", "output_index": 0, "item": {"type": "message"}},
        completed,
    ]
    payload = "".join(
        f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n" for event in events
    )
    payload += "data: [DONE]\n\n"
    return StreamingResponse(
        iter([payload.encode()]), media_type="text/event-stream", headers={"X-Request-ID": "resp_tls_network"}
    )
