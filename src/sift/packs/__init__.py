"""Domain packs.

A pack is data, not code. It declares the categories a domain cares about, what
a "case" is, how cases are keyed, which states they move through and which
categories are too expensive to get wrong. The engine below is domain-agnostic;
swapping `job-seeker` for `student-admissions` changes behaviour without a line
of Python.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from sift.errors import PackError
from sift.models import NextAction, Urgency


class CategorySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    action_required: bool = False
    urgency: Urgency = Urgency.none
    next_action: NextAction = NextAction.none
    high_stakes: bool = False        # always escalate, whatever the classifier says
    state: str | None = None         # state this category moves a case into
    opens_case: bool = False
    closes_case: bool = False
    label: str | None = None         # provider label suggestion
    prototypes: list[str] = Field(default_factory=list)  # zero-shot seed phrases


class TimedTransition(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    from_states: list[str] = Field(default_factory=list, alias="from")
    after_days_silent: int = 21


class Pack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    language: list[str] = Field(default_factory=lambda: ["en"])
    entity: str = "case"
    key: list[str] = Field(default_factory=lambda: ["org_domain", "role"])
    categories: dict[str, CategorySpec]
    states: list[str] = Field(default_factory=list)
    initial_state: str = ""
    terminal_states: list[str] = Field(default_factory=list)
    transitions: dict[str, TimedTransition] = Field(default_factory=dict)
    stall_days: int = 21
    kpis: list[str] = Field(default_factory=list)
    label_prefix: str = "Sift"

    @field_validator("categories")
    @classmethod
    def _has_other(cls, value: dict[str, CategorySpec]) -> dict[str, CategorySpec]:
        if "other" not in value:
            value["other"] = CategorySpec(description="anything the pack does not model")
        return value

    # -- convenience --------------------------------------------------------- #

    def category_names(self) -> list[str]:
        return list(self.categories)

    def spec(self, category: str) -> CategorySpec:
        return self.categories.get(category) or self.categories["other"]

    def high_stakes(self) -> set[str]:
        return {name for name, spec in self.categories.items() if spec.high_stakes}

    def state_for(self, category: str) -> str | None:
        return self.spec(category).state

    def label_for(self, category: str) -> str:
        spec = self.spec(category)
        return f"{self.label_prefix}/{spec.label or category}"

    def validate_states(self) -> None:
        known = set(self.states)
        if self.initial_state and self.initial_state not in known:
            raise PackError(f"pack '{self.name}': initial_state '{self.initial_state}' not in states")
        for name, spec in self.categories.items():
            if spec.state and spec.state not in known:
                raise PackError(
                    f"pack '{self.name}': category '{name}' maps to unknown state '{spec.state}'"
                )
        for target, transition in self.transitions.items():
            if target not in known:
                raise PackError(f"pack '{self.name}': transition target '{target}' not in states")
            for origin in transition.from_states:
                if origin not in known:
                    raise PackError(
                        f"pack '{self.name}': transition '{target}' from unknown state '{origin}'"
                    )
        for state in self.terminal_states:
            if state not in known:
                raise PackError(f"pack '{self.name}': terminal state '{state}' not in states")


def load_pack_file(path: str | Path) -> Pack:
    path = Path(path).expanduser()
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PackError(f"cannot read pack {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PackError(f"pack {path} must be a YAML mapping")
    try:
        pack = Pack(**data)
    except Exception as exc:
        raise PackError(f"invalid pack {path}: {exc}") from exc
    pack.validate_states()
    return pack


def builtin_dir() -> Path:
    return Path(str(resources.files("sift.packs").joinpath("builtin")))


def available_packs(extra_dirs: list[Path] | None = None) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for directory in [builtin_dir(), *(extra_dirs or [])]:
        if not directory.is_dir():
            continue
        for file in sorted(directory.glob("*.yaml")):
            found[file.stem] = file
    return found


@lru_cache(maxsize=16)
def _load_cached(path_str: str) -> Pack:
    return load_pack_file(path_str)


def load_pack(name_or_path: str, extra_dirs: tuple[Path, ...] = ()) -> Pack:
    """Load by pack name (builtin or from a user pack directory) or by file path."""
    candidate = Path(name_or_path).expanduser()
    if candidate.suffix in (".yaml", ".yml") and candidate.exists():
        return _load_cached(str(candidate))
    packs = available_packs(list(extra_dirs))
    if name_or_path not in packs:
        known = ", ".join(sorted(packs)) or "none"
        raise PackError(f"unknown pack '{name_or_path}'. Available: {known}")
    return _load_cached(str(packs[name_or_path]))
