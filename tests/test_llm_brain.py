"""Agent Brain tests.

Offline by default: a stub transport stands in for the Gemini endpoint, so schema
construction, parsing, guard-rails and fallback are all covered without network.
The live test runs only when GEMINI_API_KEY is set (`pytest -m live`).
"""
from __future__ import annotations

import json
import os

import httpx
import pytest

from app.agent_brain import RuleBasedAgentBrain
from app.field_registry import FieldRegistry
from app.llm_brain import GeminiAgentBrain, build_response_schema, parse_signals
from app.workflows.medical_records import AuthRequired, RecordsReceived, RequestDenied
from app.workflows.templates.outreach import OutreachSpec, OutreachTemplate
from app.workflows.registry import default_registry

TARGET = "11111111-1111-1111-1111-111111111111"
CTX = {"workflow_type": "medical_records_followup", "state": "awaiting_reply", "context": {}, "target_contact_id": TARGET}
REGISTRY = default_registry()
# client_checkin is a database row in production; register an equivalent in-process so
# these tests can assert what its extraction schema looks like without a database.
REGISTRY.register(
    OutreachTemplate(
        "client_checkin",
        OutreachSpec(message="hi", on_reply="repeat", repeat_every_days=14,
                     escalate_keywords=["pain", "hospital"]),
    )
)
FIELDS = FieldRegistry.from_dict({"contact": {"phone": {"auto_apply_threshold": 0.85}, "email": {"auto_apply_threshold": 0.85}}})


def stub_brain(responder, **kw) -> GeminiAgentBrain:
    """A GeminiAgentBrain whose HTTP calls are served by `responder(request) -> Response`."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    return GeminiAgentBrain(
        "test-key",
        domain_signals_for=lambda wt: REGISTRY.get(wt).domain_signals if wt in REGISTRY.types() else [],
        field_registry=FIELDS,
        fallback=RuleBasedAgentBrain(domain_rules_for=lambda wt: REGISTRY.get(wt).keyword_rules),
        client=client,
        **kw,
    )


def gemini_reply(signals: list[dict]) -> httpx.Response:
    body = {"candidates": [{"content": {"parts": [{"text": json.dumps({"signals": signals})}]}}]}
    return httpx.Response(200, json=body)


# ---------------------------------------------------------------- schema
def variants(schema: dict) -> dict[str, dict]:
    return {v["properties"]["type"]["enum"][0]: v for v in schema["properties"]["signals"]["items"]["anyOf"]}


def test_schema_is_generated_from_the_workflow_not_hardcoded():
    medical = build_response_schema([RecordsReceived, AuthRequired, RequestDenied])
    assert list(variants(medical)) == ["RECORDS_RECEIVED", "AUTH_REQUIRED", "REQUEST_DENIED"]

    brain = stub_brain(lambda r: gemini_reply([]))
    generic = ["RESCHEDULE", "NO_ANSWER", "ENTITY_UPDATE", "ACTION_REQUIRED", "NEEDS_HUMAN"]
    assert [c.model_fields["type"].default for c in brain.signal_classes("medical_records_followup")] == generic + [
        "RECORDS_RECEIVED", "AUTH_REQUIRED", "REQUEST_DENIED",
    ]
    assert [c.model_fields["type"].default for c in brain.signal_classes("client_checkin")] == generic + [
        "ACKNOWLEDGED", "FLAGGED",
    ]
    # each variant carries its own fields, correctly typed, with its own required list
    v = variants(build_response_schema(brain.signal_classes("medical_records_followup")))
    assert v["RESCHEDULE"]["properties"]["wait_duration"] == {"type": "string"}
    assert v["RESCHEDULE"]["required"] == ["type", "confidence", "evidence", "wait_duration"]
    assert v["NEEDS_HUMAN"]["properties"]["suggested_options"] == {"type": "array", "items": {"type": "string"}}
    assert v["NO_ANSWER"]["required"] == ["type", "confidence", "evidence"]
    # ActionRequired.details is a dict -> a spelled-out object, and required, so the
    # model cannot emit the identity of a requirement without its particulars.
    details = v["ACTION_REQUIRED"]["properties"]["details"]
    assert details["type"] == "object" and "amount" in details["properties"]
    assert set(v["ACTION_REQUIRED"]["required"]) >= {"action_type", "summary", "details"}


def test_prompt_carries_workflow_state_and_writable_fields():
    brain = stub_brain(lambda r: gemini_reply([]))
    prompt = brain._prompt(CTX, brain.signal_classes("medical_records_followup"))
    assert "medical_records_followup" in prompt and "awaiting_reply" in prompt and TARGET in prompt
    assert "email, phone" in prompt  # from the field registry, not a literal in the prompt template
    assert "HIPAA" in prompt  # domain signal docstrings reach the model


# ---------------------------------------------------------------- parsing / guard rails
def test_parse_forces_entity_update_onto_the_target_contact():
    signals = parse_signals(
        {"signals": [{"type": "ENTITY_UPDATE", "confidence": 0.9, "evidence": "call 555-0199",
                      "entity_type": "case_record", "entity_id": "99999999-9999-9999-9999-999999999999",
                      "field": "phone", "new_value": "(555) 019-9000"}]},
        [__import__("app.signals", fromlist=["EntityUpdate"]).EntityUpdate], CTX,
    )
    assert len(signals) == 1
    s = signals[0]
    assert s.entity_type == "contact" and s.entity_id == TARGET  # model cannot redirect the write
    assert s.new_value == "5550199000"  # normalised the same way the rule brain does


def test_parse_drops_unknown_and_invalid_signals():
    from app.signals import EntityUpdate, NoAnswer

    out = parse_signals(
        {"signals": [
            {"type": "MADE_UP", "confidence": 1.0},                       # unknown type
            {"type": "ENTITY_UPDATE", "confidence": 0.9},                 # missing required fields
            {"type": "NO_ANSWER", "confidence": 0.9, "evidence": "vm"},   # good
            "not-an-object",
        ]},
        [EntityUpdate, NoAnswer], CTX,
    )
    assert [s.type for s in out] == ["NO_ANSWER"]


# ---------------------------------------------------------------- end to end (stubbed)
async def test_gemini_signals_are_used_when_the_call_succeeds():
    brain = stub_brain(lambda r: gemini_reply([
        {"type": "ENTITY_UPDATE", "confidence": 0.92, "evidence": "new number is 555-0199",
         "field": "phone", "new_value": "555-0199"},
        {"type": "RESCHEDULE", "confidence": 0.88, "evidence": "not until after the holidays", "wait_duration": "3w"},
    ]))
    signals = await brain.extract_signals("our new number is 555-0199, and nothing until after the holidays", CTX)
    assert brain.last_source == "gemini"
    assert [s.type for s in signals] == ["ENTITY_UPDATE", "RESCHEDULE"]
    assert signals[0].entity_id == TARGET and signals[1].wait_duration == "3w"


async def test_request_shape_is_what_the_api_expects():
    seen: dict = {}

    def responder(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return gemini_reply([{"type": "NO_ANSWER", "confidence": 1.0, "evidence": "vm"}])

    brain = stub_brain(responder, model="gemini-3.6-flash")
    await brain.extract_signals("voicemail", CTX)
    assert seen["url"].endswith("/models/gemini-3.6-flash:generateContent")
    assert seen["key"] == "test-key"
    cfg = seen["body"]["generationConfig"]
    assert cfg["responseMimeType"] == "application/json" and cfg["temperature"] == 0
    assert list(variants(cfg["responseSchema"])) == [
        "RESCHEDULE", "NO_ANSWER", "ENTITY_UPDATE", "ACTION_REQUIRED", "NEEDS_HUMAN",
        "RECORDS_RECEIVED", "AUTH_REQUIRED", "REQUEST_DENIED",
    ]


@pytest.mark.parametrize(
    "responder",
    [
        lambda r: httpx.Response(500, text="boom"),
        lambda r: httpx.Response(429, json={"error": "rate limited"}),
        lambda r: httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}),
        lambda r: gemini_reply([]),  # valid call, nothing extracted
    ],
    ids=["http_500", "rate_limited", "bad_json", "empty_result"],
)
async def test_falls_back_to_rules_instead_of_losing_the_message(responder):
    brain = stub_brain(responder, retry_delays=(0.0,))
    signals = await brain.extract_signals("no answer, went to voicemail", CTX)
    assert brain.last_source == "fallback"
    assert [s.type for s in signals] == ["NO_ANSWER"]


async def test_transient_failures_are_retried_before_giving_up():
    """A 429 should not silently downgrade extraction to keyword matching."""
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": "rate limited"}, headers={"retry-after": "0"})
        return gemini_reply([{"type": "RESCHEDULE", "confidence": 0.9, "evidence": "two weeks", "wait_duration": "14d"}])

    brain = stub_brain(flaky, retry_delays=(0.0, 0.0))
    signals = await brain.extract_signals("call us back in two weeks", CTX)
    assert calls["n"] == 3 and brain.last_source == "gemini"
    assert [s.type for s in signals] == ["RESCHEDULE"]


async def test_retries_are_bounded_then_it_falls_back():
    calls = {"n": 0}

    def always_429(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": "rate limited"})

    brain = stub_brain(always_429, retry_delays=(0.0, 0.0))
    assert [s.type for s in await brain.extract_signals("no answer", CTX)] == ["NO_ANSWER"]
    assert calls["n"] == 3 and brain.last_source == "fallback"


async def test_client_errors_are_not_retried():
    calls = {"n": 0}

    def bad_key(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "invalid key"})

    brain = stub_brain(bad_key, retry_delays=(0.0, 0.0))
    assert [s.type for s in await brain.extract_signals("no answer", CTX)] == ["NO_ANSWER"]
    assert calls["n"] == 1, "a bad API key must not be retried"


async def test_transport_error_falls_back():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns", request=request)

    brain = stub_brain(boom, retry_delays=(0.0,))
    assert [s.type for s in await brain.extract_signals("records are attached", CTX)] == ["RECORDS_RECEIVED"]
    assert brain.last_source == "fallback"


async def test_empty_transcript_never_calls_the_api():
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not have called the API for an empty transcript")

    brain = stub_brain(explode)
    assert [s.type for s in await brain.extract_signals("   ", CTX)] == ["NO_ANSWER"]


async def test_engine_runs_with_the_llm_brain(rt, seed):
    """The Engine is brain-agnostic: swapping the implementation changes nothing."""
    from tests.helpers import load, medical_context

    rt.engine.brain = stub_brain(lambda r: gemini_reply([
        {"type": "RESCHEDULE", "confidence": 0.9, "evidence": "two weeks", "wait_duration": "14d"},
    ]))
    async with rt.session_factory() as s, s.begin():
        inst = await rt.engine.start_instance(s, "medical_records_followup", case_id=seed.case_id, context=medical_context(seed))
        iid = inst.id
    async with rt.session_factory() as s, s.begin():
        await rt.engine.handle_inbound(s, iid, "nothing for about two weeks")
    inst = await load(rt, iid)
    assert inst.wake_reason == "dynamic_reschedule" and inst.next_wake_at is not None


# ---------------------------------------------------------------- live (opt in)
@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="GEMINI_API_KEY not set")
async def test_live_gemini_extracts_a_payment_requirement_with_particulars():
    """The case that drove the discriminated-union schema: a fee with an amount and a
    payee must arrive as structured details, not prose."""
    brain = GeminiAgentBrain(
        os.environ["GEMINI_API_KEY"],
        model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
        domain_signals_for=lambda wt: REGISTRY.get(wt).domain_signals,
        field_registry=FIELDS,
    )
    signals = await brain.extract_signals(
        "There is a $45 records fee. Mail a check to Mercy Health Information Management, "
        "12 Elm St, or pay online at pay.mercy.example. We release the records once payment clears.",
        CTX,
    )
    assert brain.last_source == "gemini"
    req = next(s for s in signals if s.type == "ACTION_REQUIRED")
    assert req.action_type == "payment" and req.blocks_progress is True
    blob = " ".join(str(v) for v in req.details.values()).lower()
    assert "45" in blob and "mercy" in blob


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="GEMINI_API_KEY not set")
async def test_live_gemini_extracts_signals():
    brain = GeminiAgentBrain(
        os.environ["GEMINI_API_KEY"],
        model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
        domain_signals_for=lambda wt: REGISTRY.get(wt).domain_signals,
        field_registry=FIELDS,
    )
    signals = await brain.extract_signals(
        "Hi, this is Mercy Records. You've got the old number - use 555-0199. "
        "Also we can't pull that chart for about two weeks.", CTX,
    )
    assert brain.last_source == "gemini"
    by_type = {s.type: s for s in signals}
    assert "ENTITY_UPDATE" in by_type and by_type["ENTITY_UPDATE"].field == "phone"
    assert "5550199" in by_type["ENTITY_UPDATE"].new_value
    assert "RESCHEDULE" in by_type and by_type["RESCHEDULE"].wait_duration in {"14d", "2w"}

    checkin_ctx = {**CTX, "workflow_type": "client_checkin"}
    flagged = await brain.extract_signals("honestly the pain has been getting worse and I barely sleep", checkin_ctx)
    assert any(s.type in {"CLIENT_FLAG", "NEEDS_HUMAN"} for s in flagged)


# ---------------------------------------------------------------- who is speaking
def test_the_prompt_says_who_replied():
    """"The pain is worse" from a client is significant; from a provider's records desk
    it is usually about a patient. The brain cannot tell without being told."""
    brain = stub_brain(lambda r: gemini_reply([]))
    ctx = {**CTX, "target_contact_name": "Mercy Records", "target_contact_role": "provider"}
    prompt = brain._prompt(ctx, brain.signal_classes("medical_records_followup"))
    assert "Mercy Records, a provider" in prompt
    assert "coming from that party" in prompt


def test_the_prompt_copes_with_a_contact_it_knows_nothing_about():
    brain = stub_brain(lambda r: gemini_reply([]))
    prompt = brain._prompt(CTX, brain.signal_classes("medical_records_followup"))
    assert "the other party" in prompt


def test_a_workflows_own_note_reaches_the_prompt():
    """The outreach template builds a note from its escalation keywords; it is useless
    unless the brain actually sends it."""
    brain = stub_brain(lambda r: gemini_reply([]), prompt_notes_for=lambda wt: "treat mentions of frost as FLAGGED")
    prompt = brain._prompt(CTX, brain.signal_classes("medical_records_followup"))
    assert "This workflow adds: treat mentions of frost as FLAGGED" in prompt


# ---------------------------------------------------------------- key rotation
async def test_a_key_out_of_quota_rotates_to_the_next_one():
    """We hit 429s repeatedly on one key during development. Waiting does not fix a
    spent quota; another key does."""
    seen: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        key = request.headers["x-goog-api-key"]
        seen.append(key)
        if key == "spent":
            return httpx.Response(429, json={"error": "quota"})
        return gemini_reply([{"type": "NO_ANSWER", "confidence": 1.0, "evidence": "vm"}])

    brain = stub_brain(responder, retry_delays=(0.0,))
    brain.api_keys = ("spent", "fresh")
    signals = await brain.extract_signals("no answer", CTX)

    assert seen == ["spent", "fresh"], "rotated immediately rather than sleeping"
    assert [s.type for s in signals] == ["NO_ANSWER"] and brain.last_source == "gemini"
    assert brain.api_key == "fresh", "and stays on the key that worked"


async def test_a_rejected_key_rotates_too():
    calls: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["x-goog-api-key"])
        if request.headers["x-goog-api-key"] == "revoked":
            return httpx.Response(403, json={"error": "permission denied"})
        return gemini_reply([{"type": "NO_ANSWER", "confidence": 1.0, "evidence": "vm"}])

    brain = stub_brain(responder, retry_delays=(0.0,))
    brain.api_keys = ("revoked", "good")
    assert [s.type for s in await brain.extract_signals("no answer", CTX)] == ["NO_ANSWER"]
    assert calls == ["revoked", "good"]


async def test_when_every_key_is_spent_it_still_falls_back_to_rules():
    calls: list[str] = []

    def all_spent(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["x-goog-api-key"])
        return httpx.Response(429, json={"error": "quota"})

    brain = stub_brain(all_spent, retry_delays=(0.0,))
    brain.api_keys = ("a", "b")
    assert [s.type for s in await brain.extract_signals("no answer, voicemail", CTX)] == ["NO_ANSWER"]
    assert brain.last_source == "fallback"
    assert set(calls) == {"a", "b"}, "both were tried before giving up"


async def test_a_server_error_is_retried_on_the_same_key_not_rotated():
    """A 500 is the provider having a bad moment; a different key will not help."""
    calls: list[str] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["x-goog-api-key"])
        if len(calls) == 1:
            return httpx.Response(503, text="unavailable")
        return gemini_reply([{"type": "NO_ANSWER", "confidence": 1.0, "evidence": "vm"}])

    brain = stub_brain(flaky, retry_delays=(0.0,))
    brain.api_keys = ("a", "b")
    assert [s.type for s in await brain.extract_signals("no answer", CTX)] == ["NO_ANSWER"]
    assert calls == ["a", "a"], "same key, retried"


# ---------------------------------------------------------------- speech is a guess
def test_a_call_transcript_is_labelled_as_machine_heard():
    """It is speech recognition's guess, not a quote. The model must be told, or it
    reads garbled names and homophones as literal facts."""
    brain = stub_brain(lambda r: gemini_reply([]))
    prompt = brain._prompt({**CTX, "channel": "call"}, brain.signal_classes("medical_records_followup"))
    assert "speech recognition" in prompt
    assert "forty five dollars" in prompt, "spoken numbers must be normalised"
    assert "homophones" in prompt
    assert "below 0.85" in prompt, "and it should be less sure than with typed text"


def test_a_typed_message_is_still_read_literally():
    brain = stub_brain(lambda r: gemini_reply([]))
    prompt = brain._prompt({**CTX, "channel": "sms"}, brain.signal_classes("medical_records_followup"))
    assert "Read it literally" in prompt and "speech recognition" not in prompt
