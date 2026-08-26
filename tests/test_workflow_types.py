"""Workflow types are rows, and a row becomes a runnable definition."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.workflows.templates.outreach import OutreachTemplate
from app.workflows.types import build_definition, list_types, load_definitions, types_version, upsert_type


async def test_a_type_round_trips_from_a_row_to_a_definition(rt):
    async with rt.session_factory() as s, s.begin():
        await upsert_type(
            s, name="nudge", template="outreach",
            spec={"message": "hello {contact_name}", "retry_count": 3, "retry_interval_days": 4},
            description="a nudge", now=rt.clock.now(),
        )

    async with rt.session_factory() as s:
        definitions = await load_definitions(s)

    wf = definitions["nudge"]
    assert isinstance(wf, OutreachTemplate)
    assert wf.workflow_type == "nudge"
    assert wf.retry_policy == {"no_answer": {"schedule": ["4d", "4d", "4d"]}}


async def test_updating_a_type_replaces_its_spec(rt):
    async with rt.session_factory() as s, s.begin():
        await upsert_type(s, name="nudge", template="outreach",
                          spec={"message": "v1", "retry_count": 1}, description=None, now=rt.clock.now())
    async with rt.session_factory() as s, s.begin():
        await upsert_type(s, name="nudge", template="outreach",
                          spec={"message": "v2", "retry_count": 5}, description=None, now=rt.clock.now())

    async with rt.session_factory() as s:
        rows = await list_types(s)
        definitions = await load_definitions(s)
    assert len([r for r in rows if r.name == "nudge"]) == 1
    assert definitions["nudge"].spec.message == "v2"
    assert definitions["nudge"].retry_policy["no_answer"]["schedule"] == ["2d"] * 5


def test_an_unknown_template_is_refused():
    with pytest.raises(KeyError):
        build_definition("x", "no-such-template", {"message": "hi"})


def test_an_invalid_spec_is_refused():
    with pytest.raises(ValidationError):
        build_definition("x", "outreach", {"message": "hi", "on_reply": "repeat"})


async def test_a_spec_that_cannot_run_is_never_stored(rt):
    with pytest.raises(ValidationError):
        async with rt.session_factory() as s, s.begin():
            await upsert_type(s, name="broken", template="outreach",
                              spec={"message": "hi", "on_reply": "repeat"},
                              description=None, now=rt.clock.now())
    async with rt.session_factory() as s:
        assert "broken" not in {r.name for r in await list_types(s)}


async def test_types_version_moves_when_a_type_changes(rt):
    async with rt.session_factory() as s:
        before = await types_version(s)
    async with rt.session_factory() as s, s.begin():
        await upsert_type(s, name="nudge", template="outreach", spec={"message": "hi"},
                          description=None, now=rt.clock.now())
    async with rt.session_factory() as s:
        after = await types_version(s)
    assert after != before, "the registry uses this to decide whether to reload"
