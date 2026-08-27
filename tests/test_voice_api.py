"""The call endpoints and the socket, driven without a browser or a microphone."""
from __future__ import annotations

import json

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.main import app
from app.channels.voice import VoiceChannel
from app.db.models import CallRow
from app.runtime import VoiceStack
from app.voice.agent import ScriptedCallAgent
from app.voice.stt import ScriptedSTT
from tests.helpers import events, load, medical_context


@pytest_asyncio.fixture(loop_scope="session")
async def client(rt):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def place(rt, seed):
    rt.engine.channel = VoiceChannel(rt.channel, rt.clock, registry=rt.registry)
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(
            s, "medical_records_followup", case_id=seed.case_id, context=medical_context(seed)
        )
        iid = inst.id
    async with rt.session_factory() as s:
        call = (await s.execute(select(CallRow).where(CallRow.instance_id == iid))).scalar_one()
    return iid, str(call.id)


async def test_the_phone_page_sees_a_ringing_call(client, rt, seed):
    _iid, call_id = await place(rt, seed)

    ringing = (await client.get("/api/calls?status=ringing")).json()
    assert [c["id"] for c in ringing] == [call_id]
    assert ringing[0]["to"] == seed.provider_phone and ringing[0]["goal"]

    page = (await client.get("/phone")).text
    assert "Incoming call" in page and "Hold to talk" in page


async def test_declining_reaches_the_retry_ladder(client, rt, seed):
    """Decline is not a new path: it is a no-answer like any other."""
    iid, call_id = await place(rt, seed)

    r = await client.post(f"/api/calls/{call_id}/miss")
    assert r.status_code == 200

    inst = await load(rt, iid)
    assert inst.attempt_count == 1 and inst.wake_reason == "retry"
    assert "call_missed" in await events(rt, iid)
    assert (await client.get("/api/calls?status=ringing")).json() == []


async def test_a_call_can_be_read_back_while_it_runs(client, rt, seed):
    _iid, call_id = await place(rt, seed)
    body = (await client.get(f"/api/calls/{call_id}")).json()
    assert body["status"] == "ringing" and body["transcript"] == []
    assert (await client.get(f"/api/calls/{seed.client_id}")).status_code == 404


class FakeSocket:
    """Stands in for the browser end. Starlette's TestClient runs the app on its own
    event loop, which fights the session-scoped async fixtures, so the endpoint is
    driven directly - the protocol under test is ours, not starlette's."""

    def __init__(self, script: list[dict | bytes]):
        self._script = list(script)
        self.sent: list[dict] = []
        self.closed = False

    async def accept(self) -> None:
        pass

    async def receive(self) -> dict:
        if not self._script:
            return {"type": "websocket.disconnect"}
        item = self._script.pop(0)
        if isinstance(item, bytes):
            return {"type": "websocket.receive", "bytes": item}
        return {"type": "websocket.receive", "text": json.dumps(item)}

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m.get("type") == kind]


async def test_the_socket_runs_a_whole_call(rt, seed):
    """Answer, speak, hear a reply, hang up - over the real protocol."""
    from app.api.main import call_socket

    iid, call_id = await place(rt, seed)
    rt.voice = VoiceStack(
        stt=ScriptedSTT(["We need a signed authorization before we send those."]),
        agent=ScriptedCallAgent(["Understood - where should we send it?"]),
    )

    socket = FakeSocket([{"type": "answer"}, b"pretend-this-is-audio", {"type": "hangup"}])
    await call_socket(socket, __import__("uuid").UUID(call_id))

    spoken = [m["text"] for m in socket.of_type("speak")]
    assert spoken[0].startswith("Hi Mercy Hospital Records"), "the opening line"
    assert spoken[1] == "Understood - where should we send it?", "the agent's reply"

    heard = socket.of_type("transcript")
    assert heard and "authorization" in heard[0]["text"]

    inst = await load(rt, iid)
    assert inst.state == "awaiting_client_auth", "spoken words became signals"
    assert "call_answered" in await events(rt, iid) and "call_ended" in await events(rt, iid)


async def test_a_dropped_line_still_ends_the_call(rt, seed):
    """The browser is closed mid-call: whatever was said is still submitted."""
    from app.api.main import call_socket

    iid, call_id = await place(rt, seed)
    rt.voice = VoiceStack(
        stt=ScriptedSTT(["The records were faxed this morning."]),
        agent=ScriptedCallAgent(["Thank you."]),
    )

    # no hangup message: the script simply runs out, which is a disconnect
    socket = FakeSocket([{"type": "answer"}, b"audio"])
    await call_socket(socket, __import__("uuid").UUID(call_id))

    assert (await load(rt, iid)).status == "completed", "RECORDS_RECEIVED, from a dropped call"
    async with rt.session_factory() as s:
        call = await s.get(CallRow, __import__("uuid").UUID(call_id))
    assert call.status == "completed" and len(call.transcript) == 3


async def test_an_agent_that_ends_the_call_closes_the_socket(rt, seed):
    from app.api.main import call_socket

    _iid, call_id = await place(rt, seed)
    rt.voice = VoiceStack(stt=ScriptedSTT(["that is all, thanks"]), agent=ScriptedCallAgent([]))

    socket = FakeSocket([{"type": "answer"}, b"audio", b"never-reached"])
    await call_socket(socket, __import__("uuid").UUID(call_id))

    last = socket.of_type("speak")[-1]
    assert last["done"] is True
    assert socket.closed


async def test_the_socket_says_so_when_voice_is_off(rt, seed):
    from app.api.main import call_socket

    _iid, call_id = await place(rt, seed)
    rt.voice = None

    socket = FakeSocket([{"type": "answer"}])
    await call_socket(socket, __import__("uuid").UUID(call_id))

    assert socket.sent[0]["type"] == "error" and "not enabled" in socket.sent[0]["text"]
    assert socket.closed
