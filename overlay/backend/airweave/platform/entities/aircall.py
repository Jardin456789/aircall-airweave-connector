"""Searchable Aircall records, based on the public Calls and CI APIs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import computed_field

from airweave.platform.entities._airweave_field import AirweaveField
from airweave.platform.entities._base import BaseEntity


def _epoch(value: int | None) -> datetime | None:
    """Convert optional Unix seconds to a timezone-aware UTC datetime."""
    return datetime.fromtimestamp(value, tz=timezone.utc) if value is not None else None


class AircallCallEntity(BaseEntity):
    """One call, with notes and available post-call text; no signed audio URLs."""

    call_id: str = AirweaveField(..., description="Aircall call ID.", is_entity_id=True)
    title: str = AirweaveField(..., description="Call title.", is_name=True, embeddable=True)
    started_time: datetime | None = AirweaveField(
        None, description="Call start in UTC.", is_created_at=True
    )
    modified_time: datetime | None = AirweaveField(
        None, description="Latest known call, note, or summary time.", is_updated_at=True
    )
    direction: str = AirweaveField(..., description="Inbound or outbound.", embeddable=True)
    phone_number: str | None = AirweaveField(
        None, description="External party's raw_digits; contact may be absent.", embeddable=True
    )
    contact_name: str | None = AirweaveField(
        None, description="Contact name when present in Aircall.", embeddable=True
    )
    agent_name: str | None = AirweaveField(
        None, description="Aircall user handling the call.", embeddable=True
    )
    number_id: int | None = AirweaveField(None, description="Aircall line ID.")
    duration_seconds: int | None = AirweaveField(
        None, description="Total duration, including ringing."
    )
    talk_time_seconds: int | None = AirweaveField(
        None, description="Time from answering to ending; null for unanswered calls."
    )
    missed_call_reason: str | None = AirweaveField(
        None, description="Reason the call was missed.", embeddable=True
    )
    tags: list[str] = AirweaveField(
        default_factory=list, description="Call tag names.", embeddable=True
    )
    comments: list[str] = AirweaveField(default_factory=list, description="Agent notes.")
    language: str | None = AirweaveField(None, description="Transcript language.")
    utterances: list[dict[str, Any]] = AirweaveField(
        default_factory=list, description="Transcript passages and source timestamps."
    )
    summary: str | None = AirweaveField(None, description="Available Aircall AI summary.")
    content: str = AirweaveField(
        ..., description="Metadata, notes, summary, and transcript for search.", embeddable=True
    )

    @computed_field(return_type=str)
    def web_url(self) -> str:
        """Return the documented API reference (requires Aircall authentication)."""
        return f"https://api.aircall.io/v1/calls/{self.call_id}"

    @classmethod
    def from_api(
        cls,
        call: dict[str, Any],
        transcription: dict[str, Any] | None = None,
        summary: dict[str, Any] | None = None,
    ) -> AircallCallEntity:
        """Map Aircall objects to native fields with Airweave identity and search flags."""
        contact = call.get("contact") or {}
        contact_name = (
            " ".join(part for key in ("first_name", "last_name") if (part := contact.get(key)))
            or None
        )
        counterparty = contact_name or call.get("raw_digits") or "unknown contact"
        title = f"Aircall {call['direction']} call with {counterparty}"
        notes = call.get("comments") or []
        comments = [note["content"] for note in notes if note.get("content")]
        transcript_content = (transcription or {}).get("content") or {}
        utterances = transcript_content.get("utterances") or []
        summary_text = (summary or {}).get("content") or None
        started_time = _epoch(call.get("started_at"))
        modified_time = cls._modified_time(call, notes, summary)
        answered, ended = call.get("answered_at"), call.get("ended_at")
        return cls(
            entity_id=str(call["id"]),
            name=title,
            breadcrumbs=[],
            created_at=started_time,
            updated_at=modified_time,
            call_id=str(call["id"]),
            title=title,
            started_time=started_time,
            modified_time=modified_time,
            direction=call["direction"],
            phone_number=call.get("raw_digits"),
            contact_name=contact_name,
            agent_name=(call.get("user") or {}).get("name"),
            number_id=(call.get("number") or {}).get("id"),
            duration_seconds=call.get("duration"),
            talk_time_seconds=max(0, ended - answered)
            if answered is not None and ended is not None
            else None,
            missed_call_reason=call.get("missed_call_reason"),
            tags=[tag["name"] for tag in call.get("tags") or [] if tag.get("name")],
            comments=comments,
            language=transcript_content.get("language"),
            utterances=utterances,
            summary=summary_text,
            content=cls._content(title, started_time, comments, summary_text, utterances),
        )

    @staticmethod
    def _modified_time(
        call: dict[str, Any], notes: list[dict[str, Any]], summary: dict[str, Any] | None
    ) -> datetime | None:
        """Use provider timestamps so identical re-syncs produce identical entities."""
        times = [call.get("ended_at"), call.get("started_at")]
        times.extend(note.get("posted_at") for note in notes)
        parsed = [
            datetime.fromtimestamp(value, tz=timezone.utc) for value in times if value is not None
        ]
        if summary and summary.get("created_at"):
            timestamp = datetime.fromisoformat(summary["created_at"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            parsed.append(timestamp.astimezone(timezone.utc))
        return max(parsed) if parsed else None

    @staticmethod
    def _content(
        title: str,
        started: datetime | None,
        comments: list[str],
        summary: str | None,
        utterances: list[dict[str, Any]],
    ) -> str:
        """Render timestamped passages as embeddable text."""
        parts = [title]
        if started:
            parts.append(f"Date: {started.isoformat()}")
        if comments:
            parts.append("Notes:\n" + "\n".join(comments))
        if summary:
            parts.append(f"Summary:\n{summary}")
        passages = []
        for utterance in utterances:
            if text := (utterance.get("text") or "").strip():
                start = utterance.get("start_time")
                timestamp = f"[{start}s] " if start is not None else ""
                speaker = utterance.get("participant_type") or "speaker"
                passages.append(f"{timestamp}{speaker}: {text}")
        if passages:
            parts.append("Transcript:\n" + "\n".join(passages))
        return "\n\n".join(parts)
