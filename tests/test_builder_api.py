"""Creating a workflow type and starting an agent, without a deploy and without SQL."""
from __future__ import annotations

from datetime import timedelta

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.main import app

NUDGE = {
    "name": "deposition_reminder",
    "template": "outreach",
    "description": "Remind a witness about their deposition.",
    "spec": {
        "message": "Hi {contact_name}, reminding you about your deposition on {date}.",
        "channel": "sms",
        "retry_count": 2,
        "retry_interval_days": 3,
        "response_deadline_days": 1,
        "on_reply": "complete",
    },
}


@pytest_asyncio.fixture(loop_scope="session")
async def client(rt):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_the_catalog_distinguishes_code_from_built_types(client):
    types = {t["name"]: t for t in (await client.get("/api/workflow-types")).json()}
    assert types["medical_records_followup"]["kind"] == "code"
    assert types["medical_records_followup"]["editable"] is False
    assert types["client_checkin"]["kind"] == "template"
    assert types["client_checkin"]["editable"] is True
    assert types["client_checkin"]["spec"]["on_reply"] == "repeat"


async def test_a_type_built_over_the_api_is_immediately_runnable(client, rt, seed):
    r = await client.post("/api/workflow-types", json=NUDGE)
    assert r.status_code == 201

    catalog = (await client.get("/api/workflows")).json()
    assert catalog["deposition_reminder"]["retry_policy"] == {"no_answer": {"schedule": ["3d", "3d"]}}

    r = await client.post("/api/instances", json={
        "workflow_type": "deposition_reminder",
        "context": {"target_contact_id": str(seed.client_id), "date": "March 3rd"},
    })
    assert r.status_code == 201
    sent = rt.channel.outbox[-1]
    assert sent.body == "Hi Jane Client, reminding you about your deposition on March 3rd."
    assert sent.address == seed.client_phone


async def test_a_spec_that_cannot_run_is_refused_with_the_reason(client):
    r = await client.post("/api/workflow-types", json={
        "name": "broken", "template": "outreach", "spec": {"message": "hi", "on_reply": "repeat"},
    })
    assert r.status_code == 422
    assert "repeat_every_days" in str(r.json())

    r = await client.post("/api/workflow-types", json={
        "name": "unknown_template", "template": "nope", "spec": {"message": "hi"},
    })
    assert r.status_code == 400


async def test_a_code_defined_type_cannot_be_overwritten(client):
    r = await client.post("/api/workflow-types", json={
        "name": "medical_records_followup", "template": "outreach", "spec": {"message": "hi"},
    })
    assert r.status_code == 409


async def test_starting_an_agent_creates_the_contact_it_needs(client, rt):
    """The point of the form: a person enters a name and a number, not a UUID."""
    r = await client.post("/api/agents", json={
        "workflow_type": "client_checkin",
        "contact_name": "Marcus Bell",
        "phone": "+15550444",
        "timezone": "America/Chicago",
        "matter_type": "personal_injury",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["workflow_type"] == "client_checkin" and body["case_id"]

    sent = rt.channel.outbox[-1]
    assert sent.address == "+15550444"
    assert "Marcus Bell" in sent.body, "the message rendered the contact's name"

    facts = (await client.get(f"/api/contacts/{body['contact_id']}/facts")).json()
    assert facts == [], "a fresh contact has no corrections yet"


async def test_an_agent_needs_some_way_to_reach_the_contact(client):
    r = await client.post("/api/agents", json={"workflow_type": "client_checkin", "contact_name": "No Address"})
    assert r.status_code == 422


async def test_an_agent_of_an_unknown_type_is_refused(client):
    r = await client.post("/api/agents", json={
        "workflow_type": "nope", "contact_name": "X", "phone": "+15550000",
    })
    assert r.status_code == 400


async def test_a_built_type_runs_its_own_ladder_end_to_end(client, rt, seed):
    """Proof the spec is really driving the engine: a 3-day ladder from a form."""
    await client.post("/api/workflow-types", json=NUDGE)
    r = await client.post("/api/agents", json={
        "workflow_type": "deposition_reminder", "contact_name": "Wit Ness",
        "phone": "+15550777", "context": {"date": "March 3rd"},
    })
    iid = r.json()["id"]

    inst = (await client.get(f"/api/instances/{iid}")).json()
    assert inst["wake_reason"] == "response_timeout"

    await client.post("/api/simulate/inbound", json={"instance_id": iid, "text": "no answer"})
    inst = (await client.get(f"/api/instances/{iid}")).json()
    assert inst["attempt_count"] == 1 and inst["wake_reason"] == "retry"

    from datetime import datetime
    due = datetime.fromisoformat(inst["next_wake_at"])
    assert due - rt.clock.now() >= timedelta(days=3), "the ladder came from the form"


async def test_editing_a_type_changes_what_new_instances_do(client, rt, seed):
    await client.post("/api/workflow-types", json=NUDGE)
    edited = {**NUDGE, "spec": {**NUDGE["spec"], "message": "Rewritten: {date}", "retry_count": 1}}
    r = await client.post("/api/workflow-types", json=edited)
    assert r.status_code == 201

    catalog = (await client.get("/api/workflows")).json()
    assert catalog["deposition_reminder"]["retry_policy"] == {"no_answer": {"schedule": ["3d"]}}

    await client.post("/api/instances", json={
        "workflow_type": "deposition_reminder",
        "context": {"target_contact_id": str(seed.client_id), "date": "April 1st"},
    })
    assert rt.channel.outbox[-1].body == "Rewritten: April 1st"


# ---------------------------------------------------------------- the pages
async def test_the_builder_page_lists_both_kinds_and_creates_a_type(client, rt):
    page = (await client.get("/workflows")).text
    assert "medical_records_followup" in page and ">code<" in page
    assert "client_checkin" in page and "New workflow type" in page

    r = await client.post("/workflows", data={
        "name": "status_note", "description": "Tell a client something.",
        "message": "Hi {contact_name}, {note}", "channel": "sms",
        "retry_count": "1", "retry_interval_days": "2", "response_deadline_days": "1",
        "on_reply": "complete", "escalate_keywords": "angry, complaint",
    })
    assert r.status_code == 303 and r.headers["location"] == "/workflows"

    types = {t["name"]: t for t in (await client.get("/api/workflow-types")).json()}
    assert types["status_note"]["spec"]["escalate_keywords"] == ["angry", "complaint"]
    assert types["status_note"]["retry_policy"] == {"no_answer": {"schedule": ["2d"]}}


async def test_the_builder_reports_a_bad_spec_instead_of_swallowing_it(client):
    r = await client.post("/workflows", data={
        "name": "bad name!", "message": "hi", "on_reply": "complete",
        "retry_count": "1", "retry_interval_days": "1", "response_deadline_days": "1",
    })
    assert r.status_code in (303, 422)


async def test_the_start_form_creates_a_contact_and_runs_the_agent(client, rt):
    await client.post("/workflows", data={
        "name": "hearing_note", "message": "Hi {contact_name}, your hearing is {date}.",
        "channel": "sms", "retry_count": "1", "retry_interval_days": "2",
        "response_deadline_days": "1", "on_reply": "complete", "escalate_keywords": "",
    })

    page = (await client.get("/instances/new?workflow_type=hearing_note")).text
    assert "Start an agent" in page and "hearing_note" in page and "Timezone" in page

    r = await client.post("/instances/new", data={
        "workflow_type": "hearing_note", "contact_name": "Dana Reed", "phone": "+15550999",
        "role": "client", "timezone": "America/Chicago",
        "business_start": "09:00", "business_end": "17:00",
        "context_pairs": "date = March 3rd",
    })
    assert r.status_code == 303 and r.headers["location"].startswith("/instances/")

    sent = rt.channel.outbox[-1]
    assert sent.address == "+15550999"
    assert sent.body == "Hi Dana Reed, your hearing is March 3rd."

    instance_id = r.headers["location"].rsplit("/", 1)[-1]
    assert (await client.get(f"/instances/{instance_id}")).status_code == 200


async def test_the_start_form_says_what_is_wrong_rather_than_failing_silently(client):
    r = await client.post("/instances/new", data={
        "workflow_type": "client_checkin", "contact_name": "No Way To Reach",
        "role": "client", "timezone": "America/New_York",
        "business_start": "09:00", "business_end": "17:00", "context_pairs": "",
    })
    assert r.status_code == 303 and "error=" in r.headers["location"]
