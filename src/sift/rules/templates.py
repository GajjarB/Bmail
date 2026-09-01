"""Template recipes: the highest-value, lowest-cost stage in the system.

Applicant tracking systems send the same mail to everyone with three fields
swapped. Once a template is recognised, extraction is a regex — zero model
calls, milliseconds, and deterministic. Recipes are data files so anyone can add
one for a system they meet, without touching Python.

Recipes are matched most-specific-first; a recipe only fires when every declared
condition holds, and each condition is an any-of.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from sift.errors import RecipeError
from sift.models import Message

_FIELD_SOURCES = ("body", "subject", "from_name", "from_addr", "from_domain")


class MatchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_domain: list[str] = Field(default_factory=list)   # suffix match
    from_addr_regex: str | None = None
    subject_regex: str | None = None
    subject_any: list[str] = Field(default_factory=list)
    body_any: list[str] = Field(default_factory=list)
    body_all: list[str] = Field(default_factory=list)
    body_none: list[str] = Field(default_factory=list)   # any hit vetoes the recipe
    body_regex: str | None = None
    header_any: dict[str, str] = Field(default_factory=dict)
    language: str | None = None


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = "body"
    regex: str | None = None
    group: int = 1
    const: str | None = None
    transform: list[str] = Field(default_factory=list)


class Recipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    vendor: str = ""
    description: str = ""
    category: str
    priority: int = 50            # higher wins when several recipes match
    confidence: float = 0.95
    action_required: bool | None = None
    match: MatchSpec
    extract: dict[str, FieldSpec] = Field(default_factory=dict)

    def matches(self, message: Message) -> bool:
        spec = self.match
        haystack = f"{message.subject}\n{message.body_text}"
        lowered = haystack.lower()

        if spec.from_domain:
            domain = message.sender_domain
            if not any(domain == d or domain.endswith(f".{d}") for d in spec.from_domain):
                return False
        if spec.from_addr_regex and not re.search(
            spec.from_addr_regex, message.from_.address, re.IGNORECASE
        ):
            return False
        if spec.subject_regex and not re.search(spec.subject_regex, message.subject, re.IGNORECASE):
            return False
        if spec.subject_any and not any(
            token.lower() in message.subject.lower() for token in spec.subject_any
        ):
            return False
        if spec.body_any and not any(token.lower() in lowered for token in spec.body_any):
            return False
        if spec.body_all and not all(token.lower() in lowered for token in spec.body_all):
            return False
        if spec.body_none and any(token.lower() in lowered for token in spec.body_none):
            return False
        if spec.body_regex and not re.search(spec.body_regex, haystack, re.IGNORECASE | re.DOTALL):
            return False
        if spec.language and message.language != spec.language:
            return False
        if spec.header_any:
            headers = {k.lower(): (v or "").lower() for k, v in message.headers.items()}
            if not any(
                needle.lower() in headers.get(name.lower(), "")
                for name, needle in spec.header_any.items()
            ):
                return False
        return True

    def apply(self, message: Message) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, field in self.extract.items():
            value = self._field_value(message, field)
            if value:
                out[name] = value
        return out

    @staticmethod
    def _field_value(message: Message, field: FieldSpec) -> str | None:
        if field.const is not None:
            return field.const
        source = {
            "body": f"{message.subject}\n{message.body_text}",
            "subject": message.subject,
            "from_name": message.from_.name,
            "from_addr": message.from_.address,
            "from_domain": message.sender_domain,
        }.get(field.source)
        if source is None:
            raise RecipeError(f"unknown field source '{field.source}'")
        value = source
        if field.regex:
            match = re.search(field.regex, source, re.IGNORECASE | re.MULTILINE)
            if not match:
                return None
            try:
                value = match.group(field.group)
            except IndexError as exc:  # pragma: no cover - defensive
                raise RecipeError(f"recipe group {field.group} missing") from exc
        return _transform(value, field.transform)


def _transform(value: str, steps: list[str]) -> str | None:
    text = (value or "").strip()
    for step in steps:
        if step == "strip":
            text = text.strip(" \t\r\n-–—:;,.")
        elif step == "title":
            text = text.title()
        elif step == "collapse":
            text = re.sub(r"\s+", " ", text)
        elif step == "domain_to_org":
            text = _org_from_domain(text)
        elif step == "strip_via":
            text = _strip_via(text)
        elif step == "strip_org_suffix":
            from sift.entities.resolve import strip_role_org_suffix

            text = strip_role_org_suffix(text)
        elif step == "lower":
            text = text.lower()
        else:
            raise RecipeError(f"unknown transform '{step}'")
    return text or None


_TLD = re.compile(r"\.(com|de|org|net|io|co|eu|ai|jobs|careers|uk|at|ch|nl|fr|es|it)$")

# "Acme GmbH via Greenhouse" -> "Acme GmbH". The sending platform is not the employer.
_VIA = re.compile(
    r"\s+(?:via|through|by way of|über|ueber)\s+.+$|\s*[|·–—-]\s*(?:powered by|via)\s+.+$",
    re.IGNORECASE,
)


def _strip_via(name: str) -> str:
    return _VIA.sub("", name or "").strip(" -–—|·")


def _org_from_domain(domain: str) -> str:
    """Employer name from a sender domain — empty for anything that is not one.

    A mail from `no-reply@greenhouse.io` is not a mail from Greenhouse, and one
    from `jonas@gmx.de` is not a mail from GMX. Returning an empty string lets
    the caller fall back to the display name or give up, which is the honest
    answer; inventing "Greenhouse" as an employer poisons every case downstream.
    """
    from sift.entities.resolve import GENERIC_MAIL_DOMAINS, is_ats_domain

    domain = (domain or "").lower().strip()
    if not domain or is_ats_domain(domain) or domain in GENERIC_MAIL_DOMAINS:
        return ""
    for prefix in ("www.", "mail.", "email.", "jobs.", "careers.", "recruiting.", "no-reply."):
        if domain.startswith(prefix):
            domain = domain[len(prefix) :]
    domain = _TLD.sub("", domain)
    core = domain.split(".")[0] if "." in domain else domain
    return core.replace("-", " ").title()


@dataclass(slots=True)
class TemplateHit:
    recipe: Recipe
    fields: dict[str, str]


def recipe_dir() -> Path:
    return Path(str(resources.files("sift.rules").joinpath("recipes")))


def _load_dir(directory: Path) -> list[Recipe]:
    recipes: list[Recipe] = []
    for file in sorted(directory.glob("*.yaml")):
        try:
            data: Any = yaml.safe_load(file.read_text(encoding="utf-8")) or []
        except yaml.YAMLError as exc:
            raise RecipeError(f"cannot parse {file}: {exc}") from exc
        if not isinstance(data, list):
            raise RecipeError(f"{file} must contain a YAML list of recipes")
        for entry in data:
            try:
                recipes.append(Recipe(**entry))
            except Exception as exc:
                raise RecipeError(f"invalid recipe in {file}: {exc}") from exc
    return recipes


@lru_cache(maxsize=8)
def load_recipes(extra_dir: str | None = None) -> tuple[Recipe, ...]:
    recipes = _load_dir(recipe_dir())
    if extra_dir:
        path = Path(extra_dir).expanduser()
        if path.is_dir():
            recipes.extend(_load_dir(path))
    seen: set[str] = set()
    for recipe in recipes:
        if recipe.id in seen:
            raise RecipeError(f"duplicate recipe id '{recipe.id}'")
        seen.add(recipe.id)
    return tuple(sorted(recipes, key=lambda r: -r.priority))


def match_template(message: Message, extra_dir: str | None = None) -> TemplateHit | None:
    for recipe in load_recipes(extra_dir):
        if recipe.matches(message):
            return TemplateHit(recipe=recipe, fields=recipe.apply(message))
    return None
