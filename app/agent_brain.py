"""Agent Brain: raw message/transcript -> structured Signals.

`RuleBasedAgentBrain` is a keyword-matching baseline; `app/llm_brain.py` holds the
LLM-backed implementation. Both satisfy `AgentBrain`, so swapping is a config flag -
nothing downstream changes. The interface is async because a real brain does I/O.

workflow_context passed by the Engine:
    {"workflow_type", "instance_id", "state", "context", "target_contact_id"}
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from app.clock import duration_from_words
from app.signals import ActionRequired, EntityUpdate, NeedsHuman, NoAnswer, Reschedule, Signal


class AgentBrain(Protocol):
    async def extract_signals(self, message_text: str, workflow_context: dict[str, Any]) -> list[Signal]: ...


@dataclass(frozen=True)
class KeywordRule:
    name: str
    pattern: re.Pattern[str]
    build: Callable[[re.Match[str], str, dict[str, Any]], Signal | None]


def rule(name: str, pattern: str, build: Callable[..., Signal | None]) -> KeywordRule:
    return KeywordRule(name=name, pattern=re.compile(pattern, re.IGNORECASE), build=build)


# --- helpers -----------------------------------------------------------------------
PHONE_RE = re.compile(r"(?<!\d)(\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}|\d{3}[\s.-]\d{4})(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
AMOUNT_RE = re.compile(r"[$£€]\s?\d+(?:[.,]\d{2})?")
URL_RE = re.compile(r"\b(?:https?://)?(?:[\w-]+\.)+(?:com|org|net|gov|edu|example)(?:/\S*)?", re.IGNORECASE)


def normalize_phone(raw: str) -> str:
    return re.sub(r"[^\d+]", "", raw)


# --- generic rules (apply to every workflow) ---------------------------------------
def _entity_update_phone(m: re.Match[str], text: str, ctx: dict[str, Any]) -> Signal | None:
    phone = PHONE_RE.search(text)
    target = ctx.get("target_contact_id")
    if not phone or not target:
        return NeedsHuman(
            reason="wrong_number_no_replacement",
            suggested_options=["look up new number", "mark contact unreachable"],
            confidence=0.8,
            evidence=m.group(0),
        )
    return EntityUpdate(
        entity_type="contact",
        entity_id=str(target),
        field="phone",
        new_value=normalize_phone(phone.group(0)),
        confidence=0.9,
        evidence=text[max(0, m.start() - 10): phone.end() + 5],
    )


def _entity_update_email(m: re.Match[str], text: str, ctx: dict[str, Any]) -> Signal | None:
    target = ctx.get("target_contact_id")
    if not target:
        return None
    return EntityUpdate(
        entity_type="contact",
        entity_id=str(target),
        field="email",
        new_value=m.group("email").lower().rstrip(".,;:)"),  # sentence punctuation is not part of the address
        confidence=0.9,
        evidence=m.group(0),
    )


def _payment_required(m: re.Match[str], text: str, ctx: dict[str, Any]) -> Signal | None:
    amount = AMOUNT_RE.search(text)
    url = URL_RE.search(text)
    details = {k: v for k, v in {"amount": amount.group(0) if amount else None,
                                 "url": url.group(0) if url else None}.items() if v}
    return ActionRequired(
        action_type="payment",
        summary=f"payment required before release{f' ({amount.group(0)})' if amount else ''}",
        details=details,
        suggested_options=["pay fee", "close"],
        confidence=0.8,
        evidence=m.group(0),
    )


def _portal_required(m: re.Match[str], text: str, ctx: dict[str, Any]) -> Signal | None:
    url = URL_RE.search(text)
    return ActionRequired(
        action_type="portal_submission",
        summary="request must be submitted through their portal or records vendor",
        details={"url": url.group(0)} if url else {},
        suggested_options=["submit via portal", "call vendor"],
        confidence=0.75,
        evidence=m.group(0),
    )


def _preferred_channel(m: re.Match[str], text: str, ctx: dict[str, Any]) -> Signal | None:
    target = ctx.get("target_contact_id")
    if not target:
        return None
    # the phrase that matched is usually the refusal ("we don't take these by phone");
    # the replacement channel is stated elsewhere in the message.
    channel = "email" if (EMAIL_RE.search(text) or re.search(r"\bemail\b", text, re.IGNORECASE)) else "sms"
    return EntityUpdate(
        entity_type="contact", entity_id=str(target), field="preferred_channel", new_value=channel,
        confidence=0.85, evidence=m.group(0),
    )


def _reschedule(m: re.Match[str], text: str, ctx: dict[str, Any]) -> Signal | None:
    return Reschedule(
        wait_duration=duration_from_words(m.group("n"), m.group("unit")),
        reason=m.group(0).strip(),
        confidence=0.8,
        evidence=m.group(0),
    )


GENERIC_RULES: list[KeywordRule] = [
    rule(
        "payment_required",
        r"\b((records|processing|copying|retrieval) fee|there is a fee|fee of|invoice|prepayment|payment (is )?(required|due)|pay(ment)? (before|first)|mail a check)\b",
        _payment_required,
    ),
    rule(
        "portal_required",
        r"\b((vendor|records|request) portal|through (our|the) portal|submit (it )?(online|through|via)|release of information vendor|ciox|verisma|sharecare)\b",
        _portal_required,
    ),
    rule(
        "preferred_channel",
        r"\b((do not|don'?t|can'?t|cannot|we do not) (take|accept|handle) (these|this|requests?)?\s*(over the phone|by phone|by fax)|email (the|your) request (to|instead)|please email (us|it)|no phone requests)\b",
        _preferred_channel,
    ),
    rule(
        "wrong_number",
        r"\b(wrong number|new number|number (has )?changed|reach (me|us|him|her|them) at|call (me|us) at)\b",
        _entity_update_phone,
    ),
    rule(
        "new_email",
        # any address the other party volunteers: "email the request to x@y",
        # "our address is x@y", "reach us at x@y"
        r"(?P<email>[\w.+-]+@[\w-]+\.[\w.-]+)",
        _entity_update_email,
    ),
    rule(
        "reschedule",
        r"\b(?:in|for|after|another|within)\s+(?P<n>\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|couple of|few)\s*(?:of\s+)?(?P<unit>minutes?|hours?|days?|weeks?|months?)\b",
        _reschedule,
    ),
    rule(
        "no_answer",
        r"\b(no answer|voicemail|voice mail|no response|didn'?t pick up|did not pick up|line (was )?busy|not answered|unanswered|call dropped)\b",
        lambda m, t, c: NoAnswer(confidence=0.95, evidence=m.group(0)),
    ),
    rule(
        "needs_human",
        r"\b((speak|talk) (to|with) (a |an )?(human|person|real person|attorney|lawyer|someone)|complaint|lawsuit|sue you|supervisor)\b",
        lambda m, t, c: NeedsHuman(
            reason="caller_requested_human", suggested_options=["call back personally"], confidence=0.9, evidence=m.group(0)
        ),
    ),
]


class RuleBasedAgentBrain:
    """Keyword-matching stub. `domain_rules_for(workflow_type)` supplies the workflow's
    own rules so the brain never imports a workflow module."""

    def __init__(self, domain_rules_for: Callable[[str], list[KeywordRule]] | None = None):
        self._domain_rules_for = domain_rules_for or (lambda _wt: [])

    async def extract_signals(self, message_text: str, workflow_context: dict[str, Any]) -> list[Signal]:
        text = (message_text or "").strip()
        if not text:
            return [NoAnswer(confidence=1.0, evidence="<empty transcript>")]

        rules = list(GENERIC_RULES) + list(self._domain_rules_for(workflow_context.get("workflow_type", "")))
        signals: list[Signal] = []
        seen: set[tuple[str, str]] = set()
        for r in rules:
            m = r.pattern.search(text)
            if not m:
                continue
            sig = r.build(m, text, workflow_context)
            if sig is None:
                continue
            # keyed by (type, field) so "email us instead, at roi@..." can yield both a
            # preferred_channel change and an address change
            key = (sig.type, getattr(sig, "field", ""))
            if key in seen:
                continue
            seen.add(key)
            signals.append(sig)
        return signals
