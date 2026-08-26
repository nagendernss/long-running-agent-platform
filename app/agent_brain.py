"""Agent Brain: raw message/transcript -> structured Signals.

`RuleBasedAgentBrain` is a keyword-matching stub. Swapping in an LLM means writing
one new class that satisfies `AgentBrain` (it would build its tool schema from the
workflow's `domain_signals` instead of `keyword_rules`). Nothing downstream changes.

workflow_context passed by the Engine:
    {"workflow_type", "instance_id", "state", "context", "target_contact_id"}
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from app.signals import EntityUpdate, NeedsHuman, NoAnswer, Reschedule, Signal


class AgentBrain(Protocol):
    def extract_signals(self, message_text: str, workflow_context: dict[str, Any]) -> list[Signal]: ...


@dataclass(frozen=True)
class KeywordRule:
    name: str
    pattern: re.Pattern[str]
    build: Callable[[re.Match[str], str, dict[str, Any]], Signal | None]


def rule(name: str, pattern: str, build: Callable[..., Signal | None]) -> KeywordRule:
    return KeywordRule(name=name, pattern=re.compile(pattern, re.IGNORECASE), build=build)


# --- helpers -----------------------------------------------------------------------
_WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "couple": 2, "few": 3,
}
_UNIT_TO_SUFFIX = {"minute": "m", "hour": "h", "day": "d", "week": "w"}
PHONE_RE = re.compile(r"(?<!\d)(\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}|\d{3}[\s.-]\d{4})(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def duration_from_words(amount: str, unit: str) -> str:
    n = int(amount) if amount.isdigit() else _WORD_NUMBERS.get(amount.lower(), 1)
    unit = unit.lower().rstrip("s")
    if unit == "month":
        return f"{n * 30}d"
    return f"{n}{_UNIT_TO_SUFFIX[unit]}"


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
        new_value=m.group("email").lower(),
        confidence=0.9,
        evidence=m.group(0),
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
        "wrong_number",
        r"\b(wrong number|new number|number (has )?changed|reach (me|us|him|her|them) at|call (me|us) at)\b",
        _entity_update_phone,
    ),
    rule(
        "new_email",
        r"\b(email( address)?( is| to)?|reach (me|us) at)\s*:?\s*(?P<email>[\w.+-]+@[\w-]+\.[\w.-]+)",
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

    def extract_signals(self, message_text: str, workflow_context: dict[str, Any]) -> list[Signal]:
        text = (message_text or "").strip()
        if not text:
            return [NoAnswer(confidence=1.0, evidence="<empty transcript>")]

        rules = list(GENERIC_RULES) + list(self._domain_rules_for(workflow_context.get("workflow_type", "")))
        signals: list[Signal] = []
        seen: set[str] = set()
        for r in rules:
            m = r.pattern.search(text)
            if not m:
                continue
            sig = r.build(m, text, workflow_context)
            if sig is None or sig.type in seen:
                continue
            seen.add(sig.type)
            signals.append(sig)
        return signals
