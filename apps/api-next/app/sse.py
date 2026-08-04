import asyncio
from collections.abc import AsyncIterator

from app.store import PersistedEvent, Store, TaskIdentity


def encode_sse(event: PersistedEvent) -> str:
    import json

    data = {"task_id": str(event.task_id), "sequence": event.sequence, "payload": event.payload}
    return f"id: {event.sequence}\nevent: {event.event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def replay_and_follow(
    store: Store, task: TaskIdentity, after_sequence: int, *, follow: bool = True, poll_interval: float = 0.25
) -> AsyncIterator[str]:
    """Replay persisted events, then poll the PostgreSQL event log for live additions.

    The persistent event sequence is the resume cursor, so a disconnected browser can
    reconnect without trusting any in-memory WebSocket state.
    """
    cursor = after_sequence
    while True:
        events = await store.events_after(task, cursor)
        for event in events:
            cursor = event.sequence
            yield encode_sse(event)
        if not follow:
            return
        await asyncio.sleep(poll_interval)
