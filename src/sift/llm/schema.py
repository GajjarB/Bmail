"""JSON Schemas for constrained decoding, plus the Pydantic models that check
the values afterwards.

Two rules from the literature drive the shape of these schemas, and both are
load-bearing:

* **Keep them small and flat.** Schema compliance is essentially solved by
  constrained decoding; *value accuracy* is not, and it degrades as schemas grow
  deeper. Ten fields, no nesting.
* **Split the tasks.** Classification and extraction are separate calls with
  separate schemas. Asking one call for category, urgency, summary, org, dates
  and priority reliably produces a valid object full of worse answers.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from sift.models import NextAction, Urgency


class ClassifyOut(BaseModel):
    """Call 1: what kind of mail is this, and must the recipient do something?"""

    model_config = ConfigDict(extra="ignore")

    category: str
    action_required: bool = False
    urgency: Urgency = Urgency.none
    next_action: NextAction = NextAction.none
    summary: str = ""

    @field_validator("summary", mode="before")
    @classmethod
    def _trim(cls, value: Any) -> str:
        """Trim rather than reject.

        The schema already caps the summary during generation; if a model
        overruns it anyway, the summary is the least important field in the
        object and must not take a good classification down with it.
        """
        if value is None:
            return ""
        return " ".join(str(value).split())[:200]


class ExtractOut(BaseModel):
    """Call 2: the facts, each of which must be quotable from the email."""

    model_config = ConfigDict(extra="ignore")

    org: str | None = None
    role: str | None = None
    contact_person: str | None = None
    event_datetime: datetime | None = None
    deadline: date | None = None
    amount: str | None = None
    evidence: str | None = None

    @field_validator("org", "role", "contact_person", "amount", "evidence", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        cleaned = " ".join(value.split())
        # Models answer the question in prose when they have nothing; a valid
        # JSON string saying "not mentioned" is still a missing value.
        if cleaned.lower() in {
            "", "null", "none", "n/a", "na", "unknown", "not mentioned", "not specified",
            "not stated", "nicht angegeben", "keine angabe", "unbekannt", "-", "--",
        }:
            return None
        return cleaned

    @field_validator("event_datetime", "deadline", mode="before")
    @classmethod
    def _clean_dates(cls, value: Any) -> Any:
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned or cleaned.lower() in {"null", "none", "n/a", "not mentioned", "-"}:
                return None
            return cleaned.replace("Z", "+00:00")
        return value


def classify_schema(categories: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": categories},
            "action_required": {"type": "boolean"},
            "urgency": {"type": "string", "enum": [u.value for u in Urgency]},
            "next_action": {"type": "string", "enum": [n.value for n in NextAction]},
            "summary": {"type": "string", "maxLength": 200},
        },
        "required": ["category", "action_required", "urgency", "next_action", "summary"],
        "additionalProperties": False,
    }


def extract_schema() -> dict[str, Any]:
    nullable_string = {"type": ["string", "null"]}
    return {
        "type": "object",
        "properties": {
            "org": nullable_string,
            "role": nullable_string,
            "contact_person": nullable_string,
            "event_datetime": nullable_string,   # ISO 8601 or null
            "deadline": nullable_string,         # YYYY-MM-DD or null
            "amount": nullable_string,
            "evidence": nullable_string,
        },
        "required": [
            "org", "role", "contact_person", "event_datetime", "deadline", "amount", "evidence",
        ],
        "additionalProperties": False,
    }
