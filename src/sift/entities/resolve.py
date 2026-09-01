"""Case keys and fuzzy matching.

This is where a pile of emails becomes a tracker. Three matchers, tried in
order, because each is more reliable than the next:

1. the `References` chain — if this mail is a reply in a thread already attached
   to a case, it belongs to that case, full stop;
2. the case key — normalised organisation plus fuzzy role/programme title;
3. nothing — open a new case.

The token-set ratio is implemented here rather than pulled from a fuzzy-matching
dependency: it is twenty lines, and it keeps the core install to three packages.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

# Sending platforms. Their domain says nothing about which company is hiring, so
# the extracted organisation name must win over the sender domain for these.
ATS_DOMAINS = frozenset(
    {
        "greenhouse.io", "greenhouse-mail.io", "us.greenhouse-mail.io", "lever.co",
        "hire.lever.co", "myworkday.com", "myworkdayjobs.com", "workday.com",
        "smartrecruiters.com", "smartrecruiters.net", "workable.com", "workablemail.com",
        "ashbyhq.com", "ashbyhq.io", "recruitee.com", "teamtailor.com", "teamtailormail.com",
        "bamboohr.com", "icims.com", "taleo.net", "successfactors.com", "successfactors.eu",
        "personio.de", "personio.com", "softgarden.de", "softgarden.io", "join.com",
        "linkedin.com", "indeed.com", "indeedemail.com", "stepstone.de", "xing.com",
        "glassdoor.com", "calendly.com", "docusign.net", "hellosign.com",
    }
)

_LEGAL_SUFFIX = re.compile(
    r"\b(gmbh|mbh|ag|se|kg|ohg|ug|e\.?\s?v\.?|inc|inc\.|llc|ltd|ltd\.|limited|plc|"
    r"b\.?v\.?|n\.?v\.?|s\.?a\.?|s\.?r\.?l\.?|corp|corporation|co|company|group|holding)\b",
    re.IGNORECASE,
)

_ROLE_NOISE = re.compile(
    r"\((?:m|w|d|f|x|m/w/d|m/f/d|all genders|gn)[^)]*\)|\b(m/w/d|w/m/d|m/f/d|f/m/d|m/f/x|gn)\b",
    re.IGNORECASE,
)

_SENIORITY = re.compile(
    r"\b(senior|junior|lead|principal|staff|werkstudent|working\s+student|intern|"
    r"praktikant(?:in)?|trainee|graduate|entry[- ]level|sr\.?|jr\.?)\b",
    re.IGNORECASE,
)

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)

# "Data Engineer at Acme" / "Data Engineer bei Acme" — subject lines carry the
# employer inside the title, and it must not become part of the role key.
_ROLE_ORG_SUFFIX = re.compile(r"\s+(?:at|bei|@|for|f(?:ü|ue)r)\s+\S.*$", re.IGNORECASE)

GENERIC_MAIL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.de",
        "live.com", "yahoo.com", "yahoo.de", "gmx.de", "gmx.net", "web.de",
        "icloud.com", "me.com", "proton.me", "protonmail.com", "posteo.de", "mailbox.org",
        "t-online.de", "freenet.de", "aol.com",
    }
)


def normalise_org(name: str | None) -> str:
    """`Nordwind Analytics GmbH` and `nordwind analytics` collapse to one key."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name).lower()
    text = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    text = _LEGAL_SUFFIX.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    text = re.sub(r"\b(via|through|careers?|jobs?|recruiting|talent|hr|people)\b", " ", text)
    return " ".join(text.split())


def strip_role_org_suffix(title: str | None) -> str:
    """`Data Engineer at Acme` -> `Data Engineer`, for display as well as keys."""
    return _ROLE_ORG_SUFFIX.sub("", title or "").strip(" -–—,:")


def org_key(name: str | None) -> str:
    """The organisation part of a case key.

    Whitespace is removed, not just collapsed: the same employer reaches us as
    "Atlas Payments" from a display name and as "atlaspayments" from a domain,
    and those must land on one case rather than two.
    """
    return normalise_org(name).replace(" ", "")


def normalise_role(title: str | None) -> str:
    """Strip the gender markers and seniority noise German job titles carry."""
    if not title:
        return ""
    text = unicodedata.normalize("NFKD", title).lower()
    text = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    text = _ROLE_ORG_SUFFIX.sub(" ", text)
    text = _ROLE_NOISE.sub(" ", text)
    text = _SENIORITY.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    text = re.sub(
        r"\b(bewerbung|application|position|stelle|role|job|vacancy|als|for|the|f(?:ue|u)r)\b",
        " ",
        text,
    )
    return " ".join(text.split())


def token_set_ratio(a: str, b: str) -> int:
    """Order-insensitive similarity in 0..100, the usual fuzzy-title measure.

    `Data Engineer (m/w/d)` vs `Engineer, Data` scores high; `Data Engineer` vs
    `Data Scientist` does not.
    """
    tokens_a, tokens_b = set(a.split()), set(b.split())
    if not tokens_a or not tokens_b:
        return 100 if tokens_a == tokens_b else 0
    shared = tokens_a & tokens_b
    only_a = " ".join(sorted(tokens_a - shared))
    only_b = " ".join(sorted(tokens_b - shared))
    base = " ".join(sorted(shared))
    left = f"{base} {only_a}".strip()
    right = f"{base} {only_b}".strip()
    ratios = (
        difflib.SequenceMatcher(None, base, left).ratio(),
        difflib.SequenceMatcher(None, base, right).ratio(),
        difflib.SequenceMatcher(None, left, right).ratio(),
    )
    return int(round(max(ratios) * 100))


def org_from_domain(domain: str | None) -> str:
    if not domain:
        return ""
    domain = domain.lower()
    if domain in ATS_DOMAINS or domain in GENERIC_MAIL_DOMAINS:
        return ""
    for prefix in ("www.", "mail.", "email.", "jobs.", "careers.", "recruiting.", "no-reply."):
        if domain.startswith(prefix):
            domain = domain[len(prefix) :]
    parts = domain.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "ac", "org"):
        return parts[-3]
    return parts[0] if parts else ""


def is_ats_domain(domain: str | None) -> bool:
    domain = (domain or "").lower()
    return any(domain == known or domain.endswith(f".{known}") for known in ATS_DOMAINS)


def case_key(pack_key: list[str], values: dict[str, str | None]) -> str:
    """Build the pack-declared key. Empty parts are kept so keys stay aligned."""
    parts: list[str] = []
    for field in pack_key:
        raw = values.get(field) or ""
        if field in ("org", "org_domain", "company", "company_domain"):
            parts.append(org_key(raw) or (raw or "").lower().replace(" ", ""))
        elif field in ("role", "role_title", "programme"):
            parts.append(normalise_role(raw))
        else:
            parts.append(" ".join(str(raw).lower().split()))
    # The key keeps every declared part, including empty ones: dropping a blank
    # trailing field would change the key's arity and stop a case with no role
    # yet from matching the same case once a role is known.
    return "|".join(parts)


def keys_match(left: str, right: str, *, threshold: int = 85) -> bool:
    """Two case keys refer to the same thing if org matches exactly and the
    role is fuzzily the same. Org is never fuzzy-matched — merging two different
    employers is a far worse error than opening one duplicate card."""
    left_parts, right_parts = left.split("|"), right.split("|")
    if not left_parts or not right_parts:
        return False
    if len(left_parts) != len(right_parts):  # keys from different packs
        return False
    if left_parts[0] != right_parts[0]:
        return False
    for a, b in zip(left_parts[1:], right_parts[1:], strict=True):
        if a == b:
            continue
        if not a or not b:
            continue  # a missing role must not block the match
        if token_set_ratio(a, b) < threshold:
            return False
    return True
