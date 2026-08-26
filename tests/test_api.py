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
    # superset, not equality: registering a workflow must never break the platform's tests
    assert {"medical_records_followup", "client_checkin"} <= set(body)
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


async def test_dashboard_resolve_form_posts(client, rt, seed):
    """The dashboard's resolve buttons post a urlencoded form, not JSON - that path
    needs python-multipart installed, so it gets its own test."""
    r = await client.post(
        "/api/instances",
        json={"workflow_type": "medical_records_followup", "case_id": str(seed.case_id), "context": medical_context(seed)},
    )
    iid = r.json()["id"]
    for _ in range(4):
        await client.post("/api/simulate/inbound", json={"instance_id": iid, "text": "no answer"})
    task = next(t for t in (await client.get("/api/review-queue")).json() if t["instance_id"] == iid)

    r = await client.post(f"/review-queue/{task['id']}/resolve", data={"action": "retry"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    inst = (await client.get(f"/api/instances/{iid}")).json()
    assert inst["status"] == "active" and inst["attempt_count"] == 0 and inst["wake_reason"] == "resume_after_review"
    assert not [t for t in (await client.get("/api/review-queue")).json() if t["instance_id"] == iid]

    # and the 'close' action completes the instance
    for _ in range(4):
        await client.post("/api/simulate/inbound", json={"instance_id": iid, "text": "no answer"})
    task = next(t for t in (await client.get("/api/review-queue")).json() if t["instance_id"] == iid)
    r = await client.post(
        f"/review-queue/{task['id']}/resolve", data={"action": "close"}, headers={"referer": f"/instances/{iid}"}
    )
    assert r.status_code == 303 and r.headers["location"] == f"/instances/{iid}"
    assert (await client.get(f"/api/instances/{iid}")).json()["status"] == "completed"


async def test_error_paths(client, seed):
    assert (await client.post("/api/instances", json={"workflow_type": "nope", "context": {}})).status_code == 400
    assert (await client.get(f"/api/instances/{seed.case_id}")).status_code == 404
    r = await client.post("/api/simulate/inbound", json={"instance_id": str(seed.case_id), "text": "hi"})
    assert r.status_code == 404


async def test_dashboard_renders_the_fee_flow_and_resolves_it_with_a_reference(client, rt, seed):
    """The requirement particulars and the 'mark done & resume' box must be in the HTML,
    not just the JSON - a paralegal works from the dashboard."""
    from app.signals import ActionRequired

    r = await client.post(
        "/api/instances",
        json={"workflow_type": "medical_records_followup", "case_id": str(seed.case_id), "context": medical_context(seed)},
    )
    iid = r.json()["id"]
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine._lock_instance(s, iid)
        await rt.engine.advance_instance(s, inst, [ActionRequired(
            action_type="payment", summary="$45 records fee before release",
            details={"amount": "$45", "payee": "Mercy HIM"}, confidence=0.95, evidence="a $45 fee",
        )])

    page = (await client.get("/")).text
    assert "$45" in page and "Mercy HIM" in page, "particulars must be on the dashboard"
    assert "payment" in page and "mark done" in page

    inst_page = (await client.get(f"/instances/{iid}")).text
    assert "Waiting on us" in inst_page and "$45 records fee before release" in inst_page

    task = next(t for t in (await client.get("/api/review-queue")).json() if t["instance_id"] == iid)
    r = await client.post(
        f"/review-queue/{task['id']}/resolve",
        data={"action": "completed", "reference": "chk-1041"},
        headers={"referer": f"/instances/{iid}"},
    )
    assert r.status_code == 303

    body = (await client.get(f"/api/instances/{iid}")).json()
    assert body["status"] == "active" and "pending_requirement" not in body["context"]
    assert body["context"]["completed_requirements"][-1]["resolution"]["reference"] == "chk-1041"

    inst_page = (await client.get(f"/instances/{iid}")).text
    assert "Done on our side" in inst_page and "chk-1041" in inst_page
    assert "Waiting on us" not in inst_page

    # ...and the resumed outreach cites it
    assert [x["result"] for x in (await client.post("/api/simulate/tick")).json() if x["instance_id"] == iid] == ["executed"]
    assert "chk-1041" in rt.channel.outbox[-1].body
