"""Labelled datasets: one JSON object per line, and a locked test split.

The label file is deliberately plain JSONL so it can be edited by hand, diffed
in git and produced by `sift eval export` from your own mail. The only field
that must be present is `message_id`; everything else is optional and simply
excluded from the metrics that depend on it.

The test split is derived from a hash of the message id rather than a shuffle,
so it is stable across runs and machines. A split you can accidentally reshuffle
is a split you will accidentally train on.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sift.models import Urgency


@dataclass(slots=True)
class LabelRow:
    message_id: str
    category: str | None = None
    action_required: bool | None = None
    urgency: Urgency | None = None
    org: str | None = None
    role: str | None = None
    event_datetime: str | None = None
    deadline: str | None = None
    language: str | None = None
    notes: str = ""

    @classmethod
    def from_json(cls, data: dict) -> LabelRow:
        urgency = data.get("urgency")
        return cls(
            message_id=str(data["message_id"]),
            category=data.get("category"),
            action_required=data.get("action_required"),
            urgency=Urgency(urgency) if urgency in {u.value for u in Urgency} else None,
            org=data.get("org"),
            role=data.get("role"),
            event_datetime=data.get("event_datetime"),
            deadline=data.get("deadline"),
            language=data.get("language"),
            notes=str(data.get("notes", "")),
        )

    def to_json(self) -> dict:
        payload = {
            "message_id": self.message_id,
            "category": self.category,
            "action_required": self.action_required,
            "urgency": self.urgency.value if self.urgency else None,
            "org": self.org,
            "role": self.role,
            "event_datetime": self.event_datetime,
            "deadline": self.deadline,
            "language": self.language,
        }
        if self.notes:
            payload["notes"] = self.notes
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(slots=True)
class Dataset:
    rows: list[LabelRow] = field(default_factory=list)
    path: Path | None = None

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    @classmethod
    def load(cls, path: str | Path) -> Dataset:
        path = Path(path).expanduser()
        rows: list[LabelRow] = []
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    rows.append(LabelRow.from_json(json.loads(line)))
                except (json.JSONDecodeError, KeyError) as exc:
                    raise ValueError(f"{path}:{number}: {exc}") from exc
        return cls(rows=rows, path=path)

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path or self.path or "labels.jsonl").expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for row in self.rows:
                handle.write(json.dumps(row.to_json(), ensure_ascii=False) + "\n")
        return target

    def by_id(self) -> dict[str, LabelRow]:
        return {row.message_id: row for row in self.rows}

    def distribution(self) -> dict[str, int]:
        return dict(Counter(row.category or "unlabelled" for row in self.rows).most_common())

    # -- splitting ------------------------------------------------------------- #

    def split(self, *, test_fraction: float = 0.3, salt: str = "sift-v1") -> tuple[Dataset, Dataset]:
        """Deterministic train/test split, keyed on the message id."""
        train, test = [], []
        threshold = int(test_fraction * 1000)
        for row in self.rows:
            digest = hashlib.blake2b(f"{salt}:{row.message_id}".encode(), digest_size=4).digest()
            bucket = int.from_bytes(digest, "big") % 1000
            (test if bucket < threshold else train).append(row)
        return Dataset(train), Dataset(test)

    def stratification_report(self) -> str:
        total = len(self.rows) or 1
        lines = [f"{len(self.rows)} labelled messages"]
        for category, count in self.distribution().items():
            lines.append(f"  {category:24} {count:5}  {count / total:5.1%}")
        languages = Counter(row.language or "und" for row in self.rows)
        lines.append("  languages: " + ", ".join(f"{k}={v}" for k, v in languages.most_common()))
        return "\n".join(lines)
