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
    assert page.status_code == 200 and "How it played out" in page.text


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
    assert "<h2>Waiting on us</h2>" in inst_page and "$45 records fee before release" in inst_page

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
    # the banner clears, but the timeline keeps the history of it
    assert "<h2>Waiting on us</h2>" not in inst_page
    assert "Waiting on us — payment" in inst_page

    # ...and the resumed outreach cites it
    assert [x["result"] for x in (await client.post("/api/simulate/tick")).json() if x["instance_id"] == iid] == ["executed"]
    assert "chk-1041" in rt.channel.outbox[-1].body


async def test_instance_page_renders_the_timeline_as_rounds_and_lanes(client, rt, seed):
    """The instance page should read as a story: attempts as rounds, sends and replies
    on opposite lanes, and the wait between attempts drawn explicitly."""
    from datetime import timedelta

    r = await client.post(
        "/api/instances",
        json={"workflow_type": "medical_records_followup", "case_id": str(seed.case_id), "context": medical_context(seed)},
    )
    iid = r.json()["id"]
    await client.post("/api/simulate/inbound", json={"instance_id": iid, "text": "wrong number, use 555-0142"})
    await client.post("/api/simulate/inbound", json={"instance_id": iid, "text": "no answer"})
    rt.clock.advance(timedelta(days=3))
    await client.post("/api/simulate/tick")

    page = (await client.get(f"/instances/{iid}")).text
    assert "How it played out" in page
    assert "Attempt 1" in page and "Attempt 2" in page, "rounds come from attempt_started"
    assert "waited 2 days" in page or "waited 3 days" in page, "the wait between attempts is drawn"
    assert 'class="tl-row tl-out"' in page and 'class="tl-row tl-in"' in page, "sends and replies use different lanes"
    assert "phone applied" in page and "5550142" in page, "fact changes are legible, not raw json"
    assert "Event table" in page, "the raw table stays available"


async def test_time_travel_fires_a_long_wait_without_waiting(rt, seed):
    """A 14-day reschedule has to be testable in the running server, not only in tests."""
    from httpx import ASGITransport, AsyncClient

    from app.api.main import app
    from app.clock import OffsetClock

    original = rt.clock
    rt.clock = rt.scheduler.clock = rt.engine.clock = OffsetClock()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/api/instances",
                json={"workflow_type": "medical_records_followup", "case_id": str(seed.case_id),
                      "context": medical_context(seed)},
            )
            iid = r.json()["id"]
            await c.post("/api/simulate/inbound",
                         json={"instance_id": iid, "text": "records not available for 2 weeks"})
            assert (await c.get(f"/api/instances/{iid}")).json()["next_wake_at"] is not None
            assert (await c.post("/api/simulate/tick")).json() == [], "not due yet on real time"

            # explicit jump
            body = (await c.post("/api/simulate/advance", json={"duration": "14d"})).json()
            assert body["time_travel"] is True and body["offset_seconds"] >= 14 * 86400

            # or jump straight to whatever is scheduled next
            r = await c.post(
                "/api/instances",
                json={"workflow_type": "contact_update", "case_id": str(seed.case_id),
                      "context": {"target_contact_id": str(seed.client_id), "message": "hi", "reminder": "3d"}},
            )
            iid2 = r.json()["id"]
            body = (await c.post("/api/simulate/advance", json={})).json()
            assert body["jumped_to"] is not None
            assert [f for f in body["fired"] if f["result"] == "executed"], "the jump ran whatever was due"
            assert iid2 in {f["instance_id"] for f in body["fired"]} or (
                await c.get(f"/api/instances/{iid2}")
            ).json()["next_wake_at"] is not None

            page = (await c.get("/")).text
            assert "skip to next wake" in page and "ahead" in page

            assert (await c.post("/api/simulate/clock/reset")).json()["offset_seconds"] == 0
    finally:
        rt.clock = rt.scheduler.clock = rt.engine.clock = original


async def test_advance_is_refused_on_the_real_clock(rt, seed):
    from httpx import ASGITransport, AsyncClient

    from app.api.main import app
    from app.clock import SystemClock

    original = rt.clock
    rt.clock = rt.engine.clock = SystemClock()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/simulate/advance", json={"duration": "1d"})
            assert r.status_code == 400 and "real clock" in r.json()["detail"]
            assert (await c.get("/api/clock")).json()["time_travel"] is False
    finally:
        rt.clock = rt.engine.clock = original


async def test_review_options_are_all_actionable(client, rt, seed):
    """The queue used to print "try alternate channel" with no button behind it. Every
    option offered must now do something the engine can actually carry out."""
    from app.db.facts import get_entity_field

    r = await client.post(
        "/api/instances",
        json={"workflow_type": "medical_records_followup", "case_id": str(seed.case_id), "context": medical_context(seed)},
    )
    iid = r.json()["id"]
    for _ in range(4):
        await client.post("/api/simulate/inbound", json={"instance_id": iid, "text": "no answer"})
    task = next(t for t in (await client.get("/api/review-queue")).json() if t["instance_id"] == iid)

    page = (await client.get("/")).text
    assert 'name="channel"' in page and "try by email" in page

    # "try them another way" -> stored as a preferred_channel fact, then resume
    r = await client.post(f"/review-queue/{task['id']}/resolve", data={"action": "retry", "channel": "email"})
    assert r.status_code == 303
    async with rt.session_factory() as s:
        assert await get_entity_field(s, "contact", seed.provider_id, "preferred_channel") == "email"

    inst = (await client.get(f"/api/instances/{iid}")).json()
    assert inst["status"] == "active" and inst["attempt_count"] == 0

    rt.channel.outbox.clear()
    assert [x["result"] for x in (await client.post("/api/simulate/tick")).json() if x["instance_id"] == iid] == ["executed"]
    assert rt.channel.outbox[-1].channel == "email", "the human's channel choice is honoured on the next send"


async def test_attempts_show_the_ladder_size(client, rt, seed):
    """"attempts 2" on a two-rung ladder looks like a premature escalation until the
    denominator is visible."""
    r = await client.post(
        "/api/instances",
        json={"workflow_type": "client_checkin", "case_id": str(seed.case_id),
              "context": {"target_contact_id": str(seed.client_id), "client_contact_id": str(seed.client_id)}},
    )
    iid = r.json()["id"]
    for _ in range(3):
        await client.post("/api/simulate/inbound", json={"instance_id": iid, "text": "no answer"})

    assert (await client.get(f"/api/instances/{iid}")).json()["status"] == "blocked"
    page = (await client.get("/")).text
    assert "of 2" in page, "the dashboard shows attempts against the declared ladder"
    inst_page = (await client.get(f"/instances/{iid}")).text
    assert "of 2 before handing off" in inst_page


async def test_reply_box_drives_an_instance_from_the_page(client, rt, seed):
    """Where the payment case comes from: somebody has to reply. The instance page can
    now do that, instead of it only being reachable over the API."""
    r = await client.post(
        "/api/instances",
        json={"workflow_type": "medical_records_followup", "case_id": str(seed.case_id), "context": medical_context(seed)},
    )
    iid = r.json()["id"]
    page = (await client.get(f"/instances/{iid}")).text
    assert "Reply as" in page and 'action="/instances/' in page

    r = await client.post(
        f"/instances/{iid}/inbound",
        data={"text": "There is a $45 records fee, pay at pay.mercy.example first.", "channel": "email"},
    )
    assert r.status_code == 303

    inst = (await client.get(f"/api/instances/{iid}")).json()
    assert inst["context"]["pending_requirement"]["action_type"] == "payment"
    assert inst["status"] == "blocked"


async def test_a_reviewer_can_record_the_real_outcome(client, rt, seed):
    """Reported: a provider replied "here are your details", the brain called it
    ambiguous and escalated, and the queue only offered retry or close - neither of
    which means "the records arrived". Workflows now declare their own outcomes."""
    r = await client.post(
        "/api/instances",
        json={"workflow_type": "medical_records_followup", "case_id": str(seed.case_id), "context": medical_context(seed)},
    )
    iid = r.json()["id"]
    await client.post("/api/simulate/inbound", json={"instance_id": iid, "text": "asdf ???"})  # -> NEEDS_HUMAN
    task = next(t for t in (await client.get("/api/review-queue")).json() if t["instance_id"] == iid)
    assert {o["action"] for o in task["resolution_options"]} == {"records_received", "auth_obtained"}

    page = (await client.get("/")).text
    assert "records received" in page

    rt.channel.outbox.clear()
    r = await client.post(f"/review-queue/{task['id']}/resolve", data={"action": "records_received"})
    assert r.status_code == 303

    inst = (await client.get(f"/api/instances/{iid}")).json()
    assert inst["status"] == "completed" and inst["state"] == "completed"
    assert "sent your medical records" in rt.channel.outbox[-1].body, "the client is told, same as the signal path"


async def test_resolving_writes_one_wake_scheduled_not_two(client, rt, seed):
    """Resolving used to log the same wake twice: the resolution armed it and the
    unblock re-armed it."""
    from app.signals import ActionRequired

    r = await client.post(
        "/api/instances",
        json={"workflow_type": "medical_records_followup", "case_id": str(seed.case_id), "context": medical_context(seed)},
    )
    iid = r.json()["id"]
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine._lock_instance(s, iid)
        await rt.engine.advance_instance(s, inst, [ActionRequired(
            action_type="payment", summary="$45 fee", details={"amount": "$45"}, confidence=0.95, evidence="fee",
        )])
    task = next(t for t in (await client.get("/api/review-queue")).json() if t["instance_id"] == iid)

    before = len([e for e in (await client.get(f"/api/instances/{iid}")).json()["timeline"]
                  if e["type"] == "wake_scheduled"])
    await client.post(f"/review-queue/{task['id']}/resolve", data={"action": "completed", "reference": "chk-7"})
    after = [e for e in (await client.get(f"/api/instances/{iid}")).json()["timeline"] if e["type"] == "wake_scheduled"]
    assert len(after) == before + 1
    assert after[-1]["payload"]["reason"] == "resume_after_requirement"


async def test_header_controls_stay_reachable_while_scrolling(client, rt, seed):
    """The time-travel buttons live in the header and are used while reading a long
    timeline, so the header has to stay put."""
    page = (await client.get("/")).text
    header_css = page.split("header {")[1].split("}")[0]
    assert "position:sticky" in header_css and "top:0" in header_css
    assert "z-index" in header_css
