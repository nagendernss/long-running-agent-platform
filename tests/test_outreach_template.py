"""The outreach template: one machine, parameterised.

client_checkin and contact_update were two modules describing the same thing -
send to one contact, wait, chase on a ladder, hand off. The only real difference
is whether a positive reply finishes the instance or reschedules it.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.workflows.templates.outreach import TEMPLATES, Acknowledged, Flagged, OutreachSpec, OutreachTemplate


def test_the_ladder_is_expanded_from_a_count_and_an_interval():
    """Operators think in "chase 3 times, every 2 days", not in duration lists."""
    spec = OutreachSpec(message="hi", retry_count=3, retry_interval_days=2, response_deadline_days=2)
    assert spec.ladder() == ["2d", "2d", "2d"]

    wf = OutreachTemplate("demo", spec)
    assert wf.retry_policy == {"no_answer": {"schedule": ["2d", "2d", "2d"]}}
    assert wf.response_deadline == "2d"
    assert wf.workflow_type == "demo"


def test_no_retries_means_straight_to_a_human():
    wf = OutreachTemplate("once", OutreachSpec(message="hi", retry_count=0))
    assert wf.retry_policy == {"no_answer": {"schedule": []}}


def test_repeating_requires_a_cadence():
    with pytest.raises(ValidationError):
        OutreachSpec(message="hi", on_reply="repeat")
    spec = OutreachSpec(message="hi", on_reply="repeat", repeat_every_days=14)
    assert spec.repeat_every == "14d"


def test_keyword_rules_are_generated_from_the_spec():
    spec = OutreachSpec(message="hi", ack_phrases=["got it"], escalate_keywords=["hospital", "worse"])
    wf = OutreachTemplate("demo", spec)
    names = {r.name for r in wf.keyword_rules}
    assert names == {"acknowledged", "flagged"}

    ack = next(r for r in wf.keyword_rules if r.name == "acknowledged")
    assert ack.pattern.search("yes got it thanks") and not ack.pattern.search("nothing here")

    flag = next(r for r in wf.keyword_rules if r.name == "flagged")
    m = flag.pattern.search("I ended up in hospital")
    assert m and flag.build(m, "I ended up in hospital", {}).reason == "hospital"


def test_a_type_with_nothing_to_escalate_has_no_flag_rule():
    wf = OutreachTemplate("demo", OutreachSpec(message="hi", escalate_keywords=[]))
    assert {r.name for r in wf.keyword_rules} == {"acknowledged"}
    assert wf.domain_signals == [Acknowledged]


def test_escalation_topics_reach_the_llm_prompt():
    spec = OutreachSpec(message="hi", escalate_keywords=["pain", "hospital"])
    notes = OutreachTemplate("demo", spec).prompt_notes
    assert "pain" in notes and "hospital" in notes


def test_the_message_renders_placeholders():
    spec = OutreachSpec(message="Hi {contact_name}, about {matter}.")
    wf = OutreachTemplate("demo", spec)
    rendered = wf.render(spec.message, {"matter": "your claim"}, contact_name="Jane")
    assert rendered == "Hi Jane, about your claim."


def test_an_unknown_placeholder_is_left_alone_rather_than_exploding():
    """A builder form is written by a person; a typo must not kill a live workflow."""
    wf = OutreachTemplate("demo", OutreachSpec(message="Hi {nope}, hello"))
    assert wf.render("Hi {nope}, hello", {}, contact_name="Jane") == "Hi {nope}, hello"


def test_the_template_is_registered_by_name():
    assert TEMPLATES["outreach"] is OutreachTemplate
    assert OutreachTemplate.spec_model is OutreachSpec
