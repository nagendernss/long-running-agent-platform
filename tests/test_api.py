"""API + dashboard smoke tests. The app's lifespan is bypassed: tests reuse the
`rt` fixture's runtime (fake clock, mock channel) which the endpoints read via
get_runtime()."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.main import app
from tests.helpers import checkin_context, medical_context


@pytest_asyncio.fixture(loop_scope="session")
async def client(rt):
    transport = ASGITransport(app=app)  # no lifespan -> uses the runtime built by `rt`
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_workflow_catalog_is_generic(client):
    r = await client.get("/api/workflows")
    body = r.json()
    assert r.status_code == 200
    assert set(body) == {"medical_records_followup", "client_checkin"}
    assert body["medical_records_followup"]["retry_policy"]["no_answer"]["schedule"] == ["2d", "5d", "14d"]
    assert "RECORDS_RECEIVED" in body["medical_records_followup"]["domain_signals"]


async def test_start_inbound_timeline_and_review_flow(client, rt, seed):
    r = await client.post(
        "/api/instances",
        json={"workflow_type": "medical_records_followup", "case_id": str(seed.case_id), "context": medical_context(seed)},
    )
    assert r.status_code == 201
    iid = r.json()["id"]
    assert r.json()["state"] == "awaiting_reply"

    r = await client.post("/api/simulate/inbound", json={"instance_id": iid, "text": "records not available for 2 weeks"})
    assert [s["type"] for s in r.json()["signals"]] == ["RESCHEDULE"]
    assert r.json()["instance"]["next_wake_at"] is not None

    r = await client.get(f"/api/instances/{iid}")
    types = [e["type"] for e in r.json()["timeline"]]
    assert types[0] == "instance_created" and "wake_scheduled" in types
    assert [e["seq"] for e in r.json()["timeline"]] == sorted(e["seq"] for e in r.json()["timeline"])

    # nothing due yet, then due after the virtual clock advances
    assert (await client.post("/api/simulate/tick")).json() == []
    from datetime import timedelta

    rt.clock.advance(timedelta(days=15))
    assert [x["result"] for x in (await client.post("/api/simulate/tick")).json()] == ["executed"]

    # escalate -> review queue -> resolve
    for _ in range(4):
        await client.post("/api/simulate/inbound", json={"instance_id": iid, "text": "no answer"})
    queue = (await client.get("/api/review-queue")).json()
    assert any(t["reason"] == "stalled_no_response" and t["instance_id"] == iid for t in queue)
    task = next(t for t in queue if t["instance_id"] == iid)

    r = await client.post(f"/api/review-queue/{task['id']}/resolve", json={"resolution": {"action": "close"}, "resolved_by": "tester"})
    assert r.json()["status"] == "resolved"
    assert (await client.get(f"/api/instances/{iid}")).json()["status"] == "completed"
    assert not [t for t in (await client.get("/api/review-queue")).json() if t["instance_id"] == iid]


async def test_fact_history_endpoint(client, rt, seed):
    r = await client.post(
        "/api/instances",
        json={"workflow_type": "medical_records_followup", "case_id": str(seed.case_id), "context": medical_context(seed)},
    )
    iid = r.json()["id"]
    await client.post("/api/simulate/inbound", json={"instance_id": iid, "text": "wrong number, call 555-0199"})
    facts = (await client.get(f"/api/contacts/{seed.provider_id}/facts")).json()
    assert facts and facts[0]["field"] == "phone" and facts[0]["new_value"] == "5550199" and facts[0]["status"] == "applied"


async def test_filters_and_dashboard_pages(client, rt, seed):
    r = await client.post("/api/instances", json={"workflow_type": "client_checkin", "case_id": str(seed.case_id), "context": checkin_context(seed)})
    iid = r.json()["id"]
    listed = (await client.get("/api/instances", params={"workflow_type": "client_checkin"})).json()
    assert [i["id"] for i in listed] == [iid]
    assert (await client.get("/api/instances", params={"status": "completed"})).json() == []

    page = await client.get("/")
    assert page.status_code == 200 and "client_checkin" in page.text
    page = await client.get(f"/instances/{iid}")
    assert page.status_code == 200 and "Timeline" in page.text


async def test_error_paths(client, seed):
    assert (await client.post("/api/instances", json={"workflow_type": "nope", "context": {}})).status_code == 400
    assert (await client.get(f"/api/instances/{seed.case_id}")).status_code == 404
    r = await client.post("/api/simulate/inbound", json={"instance_id": str(seed.case_id), "text": "hi"})
    assert r.status_code == 404
