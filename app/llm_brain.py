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

import json
import logging
from typing import Any, Callable, Iterable, Sequence

import httpx
from pydantic import ValidationError

from app.agent_brain import AgentBrain, RuleBasedAgentBrain, normalize_phone
from app.field_registry import FieldRegistry
from app.signals import EntityUpdate, NeedsHuman, NoAnswer, Reschedule, Signal

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.6-flash"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"

GENERIC_SIGNAL_CLASSES: tuple[type[Signal], ...] = (Reschedule, NoAnswer, EntityUpdate, NeedsHuman)

SIGNAL_GUIDE: dict[str, str] = {
    "RESCHEDULE": "The other party asked us to come back later, or said the thing we want is not ready yet. "
                  "Set wait_duration using the compact form: 30m, 2h, 14d, 3w. Round vague phrases "
                  "('a couple of weeks' -> 14d, 'end of the month' -> 21d).",
    "NO_ANSWER": "Nobody was reached: voicemail, ringing out, busy line, an empty transcript, an auto-reply "
                 "that carries no information. Do NOT use this when the party actually replied.",
    "ENTITY_UPDATE": "The message corrects a fact we hold about the contact we are talking to (a phone number, "
                     "an email address, a preferred contact time). Only the listed fields may be updated. "
                     "new_value must be the corrected value exactly as given.",
    "NEEDS_HUMAN": "A person has to look at this: an explicit request for a human, a threat or complaint, "
                   "anything ambiguous, distressing, or outside the signals listed here. When in doubt, use this.",
}

PROMPT = """You read one inbound message or call transcript for a law firm's automated follow-up agent, and turn it into structured signals.

Workflow: {workflow_type}
Current state: {state}
Who we are contacting: contact id {target_contact_id}
Instance context: {context}

Emit every signal the message supports - a message can carry more than one (for example a corrected phone number AND a request to call back later). Emit none only if the message truly says nothing actionable.

Signals you may emit:
{signal_guide}

Rules:
- confidence is 0.0-1.0 and must reflect real certainty. Below 0.85 an ENTITY_UPDATE is held for a human to confirm, so do not inflate it.
- evidence must be a short verbatim quote from the message.
- For ENTITY_UPDATE: entity_type is "contact", entity_id is exactly {target_contact_id}, and field must be one of: {writable_fields}.
- Never invent a value that is not in the message.
- If the message is unclear, contradictory, or emotionally serious, emit NEEDS_HUMAN rather than guessing."""


def _json_type(annotation: Any) -> dict[str, Any]:
    text = str(annotation)
    if "list[str]" in text:
        return {"type": "array", "items": {"type": "string"}}
    if "float" in text:
        return {"type": "number"}
    if "int" in text and "str" not in text:
        return {"type": "integer"}
    if "bool" in text:
        return {"type": "boolean"}
    return {"type": "string"}


def build_response_schema(signal_classes: Sequence[type[Signal]]) -> dict[str, Any]:
    """One flat envelope covering every allowed signal, with `type` as the discriminator.

    A per-class anyOf would be more precise, but flat + discriminator is the shape
    every provider's structured-output mode supports, and validation happens against
    the real Pydantic model on the way back anyway.
    """
    properties: dict[str, Any] = {
        "type": {"type": "string", "enum": [c.model_fields["type"].default for c in signal_classes]},
        "confidence": {"type": "number"},
        "evidence": {"type": "string"},
    }
    for cls in signal_classes:
        for name, field in cls.model_fields.items():
            if name in ("type", "confidence", "evidence") or name in properties:
                continue
            properties[name] = _json_type(field.annotation)
    return {
        "type": "object",
        "properties": {"signals": {"type": "array", "items": {
            "type": "object", "properties": properties, "required": ["type", "confidence", "evidence"],
        }}},
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
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        domain_signals_for: Callable[[str], Iterable[type[Signal]]] | None = None,
        field_registry: FieldRegistry | None = None,
        fallback: AgentBrain | None = None,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self._domain_signals_for = domain_signals_for or (lambda _wt: [])
        self._writable_fields = sorted(field_registry.field_names()) if field_registry else ["phone", "email"]
        self.fallback = fallback or RuleBasedAgentBrain()
        self.timeout = timeout
        self._client = client
        self.last_source: str | None = None  # "gemini" | "fallback" - handy in logs/tests

    def signal_classes(self, workflow_type: str) -> list[type[Signal]]:
        return [*GENERIC_SIGNAL_CLASSES, *self._domain_signals_for(workflow_type)]

    def _prompt(self, workflow_context: dict[str, Any], signal_classes: Sequence[type[Signal]]) -> str:
        lines = []
        for cls in signal_classes:
            name = cls.model_fields["type"].default
            guide = SIGNAL_GUIDE.get(name) or (cls.__doc__ or "").strip() or f"Domain signal for {workflow_context.get('workflow_type')}."
            extra = [f for f in cls.model_fields if f not in ("type", "confidence", "evidence")]
            lines.append(f"- {name}: {guide}" + (f" Fields: {', '.join(extra)}." if extra else ""))
        return PROMPT.format(
            workflow_type=workflow_context.get("workflow_type", "unknown"),
            state=workflow_context.get("state", "unknown"),
            target_contact_id=workflow_context.get("target_contact_id", "unknown"),
            context=json.dumps(workflow_context.get("context", {}), default=str),
            signal_guide="\n".join(lines),
            writable_fields=", ".join(self._writable_fields),
        )

    async def _call(self, prompt: str, message_text: str, schema: dict[str, Any]) -> dict[str, Any]:
        body = {
            "systemInstruction": {"parts": [{"text": prompt}]},
            "contents": [{"role": "user", "parts": [{"text": f"Inbound message:\n{message_text}"}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0},
        }
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}
        url = f"{API_ROOT}/{self.model}:generateContent"
        if self._client is not None:
            response = await self._client.post(url, json=body, headers=headers, timeout=self.timeout)
        else:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=body, headers=headers)
        response.raise_for_status()
        data = response.json()
        parts = data["candidates"][0]["content"]["parts"]
        text = next(p["text"] for p in reversed(parts) if "text" in p)
        return json.loads(text)

    async def extract_signals(self, message_text: str, workflow_context: dict[str, Any]) -> list[Signal]:
        text = (message_text or "").strip()
        if not text:  # nothing to reason about - no point paying for a call
            self.last_source = "fallback"
            return [NoAnswer(confidence=1.0, evidence="<empty transcript>")]

        classes = self.signal_classes(workflow_context.get("workflow_type", ""))
        try:
            payload = await self._call(self._prompt(workflow_context, classes), text, build_response_schema(classes))
            signals = parse_signals(payload, classes, workflow_context)
            if signals:
                self.last_source = "gemini"
                return signals
            log.info("gemini returned no usable signal, falling back to rules")
        except Exception as exc:  # transport, HTTP status, malformed JSON - all handled the same
            log.warning("gemini brain failed (%s: %s), falling back to rules", type(exc).__name__, exc)

        self.last_source = "fallback"
        return await self.fallback.extract_signals(text, workflow_context)
