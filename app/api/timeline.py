"""Turns the raw `event` rows into something a paralegal can read at a glance.

Two ideas shape it:

* **Rounds, not a list.** A long-running instance is a loop - attempt, outcome, wait,
  attempt. `attempt_started` opens a round, so the numbering means something real
  (it is the attempt counter), rather than decorating the page.
* **Lanes, because direction matters.** What we sent and what came back are the two
  halves of a conversation; engine decisions sit between them on the spine. Waits
  between rounds are drawn as gaps, because on this platform the elapsed time *is*
  the substance - a case can sit for three weeks between two lines.

Presentation only. Nothing here is imported by the engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.db.models import Event

OUTBOUND = {"message_sent", "message_failed"}
INBOUND = {"inbound_received"}


@dataclass
class Node:
    kind: str                       # out | in | engine
    tone: str                       # accent | reply | fact | wait | alert | done | muted
    glyph: str
    label: str
    detail: str = ""
    chips: list[str] = field(default_factory=list)
    at: datetime | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Round:
    index: int | None               # attempt number; None for the pre-attempt round
    title: str
    subtitle: str
    nodes: list[Node] = field(default_factory=list)
    gap_before: str = ""            # "waited 14 days" - rendered on the connector


def _short(text: str, limit: int = 110) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _human_gap(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min" if minutes else ""
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    if days < 14:
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{days // 7} weeks"


def _when(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%b %d, %H:%M")
    except ValueError:
        return str(value)


def node_for(event: Event) -> Node | None:
    """One event -> one node. Returns None for events that only exist to open a round."""
    p = event.payload or {}
    t = event.type

    if t == "attempt_started":
        return None

    if t == "call_placed":
        return Node("out", "accent", "☎", "Called", p.get("to", ""), [p.get("goal_summary", "")] if p.get("goal_summary") else [], event.created_at, p)

    if t == "call_answered":
        return Node("engine", "reply", "✓", "They answered", "", [], event.created_at, p)

    if t == "call_missed":
        return Node("engine", "muted", "⊘", "Nobody answered", p.get("reason", ""), [], event.created_at, p)

    if t == "call_ended":
        turns = p.get("turns")
        return Node(
            "engine", "muted", "•", "Call ended", p.get("reason", ""),
            [f"{turns} turns"] if turns else [], event.created_at, p,
        )

    if t == "call_failed":
        return Node("engine", "alert", "!", "The call failed", p.get("reason", ""), [], event.created_at, p)

    if t == "instance_created":
        return Node("engine", "muted", "◆", "Instance created", p.get("workflow_type", ""), at=event.created_at, payload=p)

    if t == "message_sent":
        return Node(
            "out", "accent", "↑", f"Sent by {p.get('channel', 'message')}",
            _short(p.get("body", "")), [f"to {p.get('to', '?')}"], event.created_at, p,
        )

    if t == "message_failed":
        return Node("out", "alert", "⊘", "Could not send", p.get("reason", ""), [], event.created_at, p)

    if t == "inbound_received":
        return Node(
            "in", "reply", "↓", f"Reply on {p.get('channel', 'message')}",
            _short(p.get("text", "")), [], event.created_at, p,
        )

    if t == "signals_extracted":
        return Node(
            "engine", "muted", "◈", "Read as",
            "", [s.get("type", "?") for s in p.get("signals", [])], event.created_at, p,
        )

    if t == "outcome_recorded":
        bits = []
        if p.get("retry_in"):
            bits.append(f"retry in {p['retry_in']}")
        if p.get("wait"):
            bits.append(f"wait {p['wait']}")
        if p.get("retry") == "exhausted":
            bits.append("retry schedule exhausted")
        return Node("engine", "muted", "·", f"Outcome: {p.get('outcome', '?')}", ", ".join(bits), [], event.created_at, p)

    if t == "wake_scheduled":
        return Node(
            "engine", "wait", "◷", "Next attempt scheduled",
            _when(p.get("at")), [p.get("reason", "")], event.created_at, p,
        )

    if t == "wake_skipped":
        return Node("engine", "muted", "⊘", "Wake skipped", p.get("reason", ""), [], event.created_at, p)

    if t.startswith("fact_"):
        status = t.split("_", 1)[1]
        arrow = f"{p.get('old_value') or '—'} → {p.get('new_value')}"
        return Node(
            "engine", "fact", "Δ", f"{p.get('field', 'fact')} {status}",
            arrow, [f"confidence {p.get('confidence')}"], event.created_at, p,
        )

    if t == "channel_overridden":
        return Node(
            "engine", "fact", "⇄", "Channel switched",
            f"{p.get('requested')} → {p.get('used')}", ["their stated preference"], event.created_at, p,
        )

    if t == "action_required":
        details = p.get("details") or {}
        return Node(
            "engine", "alert", "!", f"Waiting on us — {p.get('action_type', 'action')}",
            p.get("summary", ""), [f"{k}: {v}" for k, v in details.items()], event.created_at, p,
        )

    if t == "requirement_completed":
        ref = (p.get("resolution") or {}).get("reference")
        return Node(
            "engine", "done", "✓", "Done on our side", p.get("summary", ""),
            ([f"ref {ref}"] if ref else []) + [f"by {p.get('completed_by', '?')}"], event.created_at, p,
        )

    if t == "escalated":
        return Node(
            "engine", "alert", "!", "Handed to a human", p.get("reason", ""),
            p.get("suggested_options") or [], event.created_at, p,
        )

    if t == "review_resolved":
        res = p.get("resolution") or {}
        return Node(
            "engine", "done", "✓", f"Human decision: {res.get('action', '?')}",
            p.get("reason", ""), [f"by {p.get('by', '?')}"], event.created_at, p,
        )

    if t == "state_changed":
        return Node("engine", "muted", "◇", "State", f"{p.get('from')} → {p.get('to')}", [], event.created_at, p)

    if t == "completed":
        return Node("engine", "done", "●", "Completed", p.get("outcome", ""), [], event.created_at, p)

    return Node("engine", "muted", "·", t.replace("_", " "), "", [], event.created_at, p)


def build_rounds(events: list[Event]) -> list[Round]:
    rounds: list[Round] = []
    current = Round(index=None, title="Opened", subtitle="")

    for event in events:
        if event.type == "attempt_started":
            if current.nodes or rounds:
                rounds.append(current)
            p = event.payload or {}
            attempt = p.get("attempt", 0)
            reason = (p.get("reason") or "wake").replace("_", " ")
            current = Round(
                index=attempt + 1,
                title=f"Attempt {attempt + 1}",
                subtitle=reason,
            )
            current.at = event.created_at  # type: ignore[attr-defined]
            continue
        node = node_for(event)
        if node:
            current.nodes.append(node)

    rounds.append(current)

    # Draw the wait between rounds - the thing that makes these workflows unusual.
    previous_end: datetime | None = None
    for r in rounds:
        first = r.nodes[0].at if r.nodes else None
        if previous_end and first:
            gap = _human_gap(first - previous_end)
            if gap and (first - previous_end) >= timedelta(hours=1):
                r.gap_before = gap
        if r.nodes:
            previous_end = r.nodes[-1].at
    return [r for r in rounds if r.nodes]
