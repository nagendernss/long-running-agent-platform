"""LLM-backed Agent Brain (Google Gemini).

Same `AgentBrain` interface as the rule-based baseline, so switching is a config
flag (`AGENT_BRAIN=gemini`) and nothing downstream changes.

Two things keep this honest for a long-running platform:

* **The signal catalogue is generated, not hardcoded.** The JSON schema handed to
  the model is built from the generic signal classes plus the workflow's own
  `domain_signals`, and the writable-field enum comes from the Field Registry. A
  new workflow (or a new registry entry) changes the prompt automatically.
* **It always degrades, never drops.** Any transport error, bad JSON, or empty
  extraction falls back to the rule-based brain; if that also finds nothing the
  Engine turns the empty list into NEEDS_HUMAN. A provider outage slows the
  platform down, it does not lose a client's reply.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Iterable, Sequence

import httpx
from pydantic import ValidationError

from app import gemini as gemini_module
from app.gemini import GeminiClient

from app.agent_brain import AgentBrain, RuleBasedAgentBrain, normalize_phone
from app.field_registry import FieldRegistry
from app.signals import ActionRequired, EntityUpdate, NeedsHuman, NoAnswer, Reschedule, Signal

log = logging.getLogger(__name__)

DEFAULT_MODEL = gemini_module.DEFAULT_MODEL
RETRY_DELAYS = gemini_module.RETRY_DELAYS

GENERIC_SIGNAL_CLASSES: tuple[type[Signal], ...] = (Reschedule, NoAnswer, EntityUpdate, ActionRequired, NeedsHuman)

SIGNAL_GUIDE: dict[str, str] = {
    "RESCHEDULE": "The other party asked us to come back later, or said the thing we want is not ready yet. "
                  "Set wait_duration using the compact form: 30m, 2h, 14d, 3w. Round vague phrases "
                  "('a couple of weeks' -> 14d, 'end of the month' -> 21d).",
    "NO_ANSWER": "Nobody was reached: voicemail, ringing out, busy line, an empty transcript, an auto-reply "
                 "that carries no information. Do NOT use this when the party actually replied.",
    "ENTITY_UPDATE": "The message corrects a fact we hold about the contact we are talking to: a phone number, "
                     "an email address, a preferred contact time, or how they want to be reached "
                     "(preferred_channel, one of: call, sms, email - use this when they say 'do not phone us, "
                     "email the request instead'). Only the listed fields may be updated, and new_value must be "
                     "the corrected value exactly as given. Emit one ENTITY_UPDATE per field.",
    "ACTION_REQUIRED": "They will not proceed until WE do something first: pay a fee, submit through their "
                       "portal or a records vendor, send a form or an ID. Set action_type to one of payment, "
                       "portal_submission, form, document, other, and put a one-line instruction in summary. "
                       "ALWAYS fill the details object with every particular the message states - amount, "
                       "currency, payee, url, address, deadline, reference - because a paralegal acts on details "
                       "alone. Example: 'there is a $45 fee, mail a check to Mercy HIM, 12 Elm St' -> "
                       "action_type 'payment', details {amount: '$45', payee: 'Mercy HIM', address: '12 Elm St'}. "
                       "Use this rather than NEEDS_HUMAN whenever the required action is clear.",
    "NEEDS_HUMAN": "A person has to look at this: an explicit request for a human, a threat or complaint, "
                   "anything ambiguous, distressing, or outside the signals listed here. When in doubt, use this.",
}

PROMPT = """You read one inbound message or call transcript for a law firm's automated follow-up agent, and turn it into structured signals.

Workflow: {workflow_type}
Current state: {state}
Who replied: {target_description} (contact id {target_contact_id})
Instance context: {context}

Emit every signal the message supports - a message can carry more than one (for example a corrected phone number AND a request to call back later). Emit none only if the message truly says nothing actionable.

Signals you may emit:
{signal_guide}

Rules:
- confidence is 0.0-1.0 and must reflect real certainty. Below 0.85 an ENTITY_UPDATE is held for a human to confirm, so do not inflate it.
- evidence must be a short verbatim quote from the message.
- For ENTITY_UPDATE: entity_type is "contact", entity_id is exactly {target_contact_id}, and field must be one of: {writable_fields}.
- Never invent a value that is not in the message.
- Read the message as coming from that party. A client describing their own symptoms, distress or dissatisfaction is significant; a provider's office using the same words is usually describing a patient or their process, not themselves.
- If the message is unclear, contradictory, or emotionally serious, emit NEEDS_HUMAN rather than guessing.
- A message can both correct a fact and impose a requirement (for example "we do not take these by phone, email the request to x@y and there is a $45 fee") - emit ENTITY_UPDATE and ACTION_REQUIRED together."""


# Gemini's schema subset needs an object's properties spelled out, so a free-form
# `dict` field (ActionRequired.details) is declared as this fixed set of particulars.
# Anything outside it still reaches a human through `summary` and `evidence`.
DETAIL_PROPERTIES: dict[str, Any] = {
    "amount": {"type": "string"},
    "currency": {"type": "string"},
    "payee": {"type": "string"},
    "url": {"type": "string"},
    "address": {"type": "string"},
    "deadline": {"type": "string"},
    "reference": {"type": "string"},
    "notes": {"type": "string"},
}


def _describe_target(workflow_context: dict[str, Any]) -> str:
    name = workflow_context.get("target_contact_name")
    role = workflow_context.get("target_contact_role")
    if name and role:
        return f"{name}, a {role}"
    return name or (f"a {role}" if role else "the other party")


def _json_type(annotation: Any) -> dict[str, Any]:
    text = str(annotation)
    if "list[str]" in text:
        return {"type": "array", "items": {"type": "string"}}
    if text.startswith("dict") or "dict[" in text:
        return {"type": "object", "properties": DETAIL_PROPERTIES}
    if "float" in text:
        return {"type": "number"}
    if "int" in text and "str" not in text:
        return {"type": "integer"}
    if "bool" in text:
        return {"type": "boolean"}
    return {"type": "string"}


def _class_schema(cls: type[Signal]) -> dict[str, Any]:
    """One schema variant per signal class, carrying that class's own required fields."""
    properties: dict[str, Any] = {
        "type": {"type": "string", "enum": [cls.model_fields["type"].default]},
        "confidence": {"type": "number"},
        "evidence": {"type": "string"},
    }
    required = ["type", "confidence", "evidence"]
    for name, field in cls.model_fields.items():
        if name in properties:
            continue
        properties[name] = _json_type(field.annotation)
        # `details` carries the particulars a paralegal acts on, so require it even
        # though the Python model defaults it to {}.
        if field.is_required() or name == "details":
            required.append(name)
    return {"type": "object", "properties": properties, "required": required}


def build_response_schema(signal_classes: Sequence[type[Signal]]) -> dict[str, Any]:
    """A discriminated union: one variant per signal, each with its own required fields.

    A single flat envelope was tried first and failed in practice - the model split
    one ACTION_REQUIRED across two objects (identity in the first, `details` in the
    second), and the half without `action_type` was dropped on validation. Per-class
    `required` makes that shape impossible to emit.
    """
    return {
        "type": "object",
        "properties": {"signals": {"type": "array", "items": {"anyOf": [_class_schema(c) for c in signal_classes]}}},
        "required": ["signals"],
    }


def parse_signals(
    payload: dict[str, Any], signal_classes: Sequence[type[Signal]], workflow_context: dict[str, Any]
) -> list[Signal]:
    """Validate the model's output against the real Signal classes. Anything that does
    not validate is dropped with a log line rather than crashing the inbound path."""
    by_type = {c.model_fields["type"].default: c for c in signal_classes}
    target = workflow_context.get("target_contact_id")
    out: list[Signal] = []
    for raw in payload.get("signals") or []:
        if not isinstance(raw, dict):
            continue
        cls = by_type.get(raw.get("type"))
        if cls is None:
            log.warning("brain returned unknown signal type %r", raw.get("type"))
            continue
        data = {k: v for k, v in raw.items() if k in cls.model_fields and v is not None}
        if cls is EntityUpdate:
            # The model must not choose which entity we are updating: it is always the
            # contact this instance is talking to.
            data["entity_type"] = "contact"
            data["entity_id"] = str(target) if target else data.get("entity_id", "")
            if data.get("field") == "phone" and data.get("new_value"):
                data["new_value"] = normalize_phone(str(data["new_value"]))
        try:
            out.append(cls(**data))
        except ValidationError as exc:
            log.warning("brain returned an invalid %s: %s", raw.get("type"), exc)
    return out


class GeminiAgentBrain:
    """Gemini-backed brain with a rule-based fallback."""

    def __init__(
        self,
        api_key: str | Sequence[str],
        *,
        model: str = DEFAULT_MODEL,
        domain_signals_for: Callable[[str], Iterable[type[Signal]]] | None = None,
        prompt_notes_for: Callable[[str], str | None] | None = None,
        field_registry: FieldRegistry | None = None,
        fallback: AgentBrain | None = None,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
        retry_delays: Sequence[float] = RETRY_DELAYS,
    ):
        self.gemini = GeminiClient(api_key, model=model, timeout=timeout,
                                   retry_delays=retry_delays, client=client)
        self.model = model
        self._domain_signals_for = domain_signals_for or (lambda _wt: [])
        self._prompt_notes_for = prompt_notes_for or (lambda _wt: None)
        self._writable_fields = sorted(field_registry.field_names()) if field_registry else ["phone", "email"]
        self.fallback = fallback or RuleBasedAgentBrain()
        self.last_source: str | None = None  # "gemini" | "fallback" - handy in logs/tests

    # the keys live on the shared client; these keep the brain's own surface familiar
    @property
    def api_keys(self) -> tuple[str, ...]:
        return self.gemini.api_keys

    @api_keys.setter
    def api_keys(self, keys) -> None:
        self.gemini.api_keys = tuple(keys)
        self.gemini._key_index = 0

    @property
    def api_key(self) -> str:
        return self.gemini.api_key

    def signal_classes(self, workflow_type: str) -> list[type[Signal]]:
        return [*GENERIC_SIGNAL_CLASSES, *self._domain_signals_for(workflow_type)]

    def _prompt(self, workflow_context: dict[str, Any], signal_classes: Sequence[type[Signal]]) -> str:
        lines = []
        for cls in signal_classes:
            name = cls.model_fields["type"].default
            guide = SIGNAL_GUIDE.get(name) or (cls.__doc__ or "").strip() or f"Domain signal for {workflow_context.get('workflow_type')}."
            extra = [f for f in cls.model_fields if f not in ("type", "confidence", "evidence")]
            lines.append(f"- {name}: {guide}" + (f" Fields: {', '.join(extra)}." if extra else ""))
        notes = self._prompt_notes_for(workflow_context.get("workflow_type", ""))
        if notes:
            lines.append("")
            lines.append(f"This workflow adds: {notes}")
        return PROMPT.format(
            workflow_type=workflow_context.get("workflow_type", "unknown"),
            state=workflow_context.get("state", "unknown"),
            target_description=_describe_target(workflow_context),
            target_contact_id=workflow_context.get("target_contact_id", "unknown"),
            context=json.dumps(workflow_context.get("context", {}), default=str),
            signal_guide="\n".join(lines),
            writable_fields=", ".join(self._writable_fields),
        )

    async def _call_with_retry(self, prompt: str, message_text: str, schema: dict[str, Any]) -> dict[str, Any]:
        return await self.gemini.generate_json(
            system=prompt, user="Inbound message:\n" + message_text, schema=schema
        )

    async def extract_signals(self, message_text: str, workflow_context: dict[str, Any]) -> list[Signal]:
        text = (message_text or "").strip()
        if not text:  # nothing to reason about - no point paying for a call
            self.last_source = "fallback"
            return [NoAnswer(confidence=1.0, evidence="<empty transcript>")]

        classes = self.signal_classes(workflow_context.get("workflow_type", ""))
        try:
            payload = await self._call_with_retry(
                self._prompt(workflow_context, classes), text, build_response_schema(classes)
            )
            signals = parse_signals(payload, classes, workflow_context)
            if signals:
                self.last_source = "gemini"
                return signals
            log.info("gemini returned no usable signal, falling back to rules")
        except Exception as exc:  # transport, HTTP status, malformed JSON - all handled the same
            log.warning("gemini brain failed (%s: %s), falling back to rules", type(exc).__name__, exc)

        self.last_source = "fallback"
        return await self.fallback.extract_signals(text, workflow_context)
