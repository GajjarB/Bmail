"""A synthetic corpus, so the pipeline is runnable before you connect any account.

The generator is seeded and deterministic, mixes English and German, and writes
label ground truth alongside the mbox — which makes it an honest smoke test and
a first target for the evaluation harness.

It is not a substitute for your own mail. Real corpora are messier in ways no
generator invents; the roadmap is explicit that you label a few hundred of your
own messages before trusting any number.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

COMPANIES = [
    ("Nordwind Analytics", "nordwind-analytics.de", "de"),
    ("Helios Robotics", "heliosrobotics.com", "en"),
    ("Bergblick Software", "bergblick.de", "de"),
    ("Cobalt Health", "cobalthealth.com", "en"),
    ("Fraunhofer Institut IPT", "ipt-example.de", "de"),
    ("Lumen Mobility", "lumenmobility.eu", "en"),
    ("Weser Data GmbH", "weserdata.de", "de"),
    ("Atlas Payments", "atlaspayments.com", "en"),
]

UNIVERSITIES = [
    ("Technische Universität Rheinland", "tu-rheinland-example.de", "de"),
    ("University of Groningen", "rug-example.nl", "en"),
    ("RWTH Beispiel", "rwth-example.de", "de"),
]

ROLES = [
    "Data Engineer", "Machine Learning Engineer", "Backend Developer",
    "Werkstudent Data Science", "Software Engineer (m/w/d)", "Platform Engineer",
]

PROGRAMMES = ["MSc Data Science", "MSc Computer Science", "MSc Artificial Intelligence"]

ATS_SENDERS = [
    ("no-reply@greenhouse.io", "Greenhouse"),
    ("no-reply@hire.lever.co", "Lever"),
    ("noreply@myworkday.com", "Workday"),
    ("bewerbung@personio.de", "Personio"),
    ("no-reply@softgarden.de", "softgarden"),
]


@dataclass(slots=True)
class Label:
    message_id: str
    category: str
    action_required: bool
    org: str
    role: str
    language: str = "en"
    deadline: str | None = None
    event_datetime: str | None = None


def _stable_id(rng: random.Random) -> str:
    """A Message-ID drawn from the seeded generator.

    `email.utils.make_msgid` mixes in the clock and os.urandom, which would make
    every run produce different ids — and the evaluation test split is keyed on
    the message id, so two runs of a "seeded" corpus would not be comparable.
    """
    return f"<{rng.getrandbits(64):016x}.{rng.getrandbits(32):08x}@sift.example>"


def _msg(
    *,
    subject: str,
    body: str,
    from_name: str,
    from_addr: str,
    to_addr: str,
    when: datetime,
    rng: random.Random,
    extra_headers: dict[str, str] | None = None,
    references: list[str] | None = None,
) -> tuple[EmailMessage, str]:
    message = EmailMessage()
    message_id = _stable_id(rng)
    message["Message-ID"] = message_id
    message["Subject"] = subject
    message["From"] = f"{from_name} <{from_addr}>"
    message["To"] = to_addr
    message["Date"] = format_datetime(when)
    if references:
        message["References"] = " ".join(references)
        message["In-Reply-To"] = references[-1]
    for name, value in (extra_headers or {}).items():
        message[name] = value
    message.set_content(body)
    return message, message_id


def generate_corpus(
    *,
    seed: int = 7,
    applications: int = 24,
    noise: int = 40,
    owner: str = "you@example.com",
    end: datetime | None = None,
) -> tuple[list[EmailMessage], list[Label]]:
    """Build a mixed EN/DE corpus of application lifecycles plus realistic noise."""
    rng = random.Random(seed)
    end = end or datetime.now(timezone.utc)
    messages: list[EmailMessage] = []
    labels: list[Label] = []

    def emit(message: EmailMessage, message_id: str, category: str, action: bool,
             org: str, role: str, language: str = "en", deadline: str | None = None,
             event: str | None = None) -> None:
        messages.append(message)
        labels.append(Label(message_id, category, action, org, role, language, deadline, event))

    for index in range(applications):
        company, domain, lang = COMPANIES[index % len(COMPANIES)]
        role = ROLES[index % len(ROLES)]
        ats_addr, ats_name = ATS_SENDERS[index % len(ATS_SENDERS)]
        applied = end - timedelta(days=rng.randint(5, 120), hours=rng.randint(0, 20))
        thread: list[str] = []

        # 1. acknowledgement, always
        if lang == "de":
            subject = f"Ihre Bewerbung als {role} bei {company}"
            body = (
                f"Sehr geehrte Bewerberin, sehr geehrter Bewerber,\n\n"
                f"vielen Dank für Ihre Bewerbung als {role} bei {company}. "
                f"Wir haben Ihre Bewerbung erhalten und werden Ihre Unterlagen sorgfältig prüfen.\n\n"
                f"Ihre Referenznummer lautet {rng.randint(100000, 999999)}.\n\n"
                f"Mit freundlichen Grüßen\nDas Recruiting-Team von {company}"
            )
        else:
            subject = f"Your application for {role} at {company}"
            body = (
                f"Hello,\n\nThank you for applying to {company}. We have received your "
                f"application for the {role} position and our team will review it shortly.\n\n"
                f"Reference: {rng.randint(100000, 999999)}\n\nBest regards,\n{company} Talent Team"
            )
        message, message_id = _msg(
            subject=subject, body=body, from_name=f"{company} via {ats_name}",
            from_addr=ats_addr, to_addr=owner, when=applied, rng=rng,
            extra_headers={"Auto-Submitted": "auto-generated"},
        )
        thread.append(message_id)
        emit(message, message_id, "acknowledgement", False, company, role, lang)

        outcome = rng.random()

        # 2. an optional assessment step
        if outcome > 0.55:
            when = applied + timedelta(days=rng.randint(2, 10))
            due = when + timedelta(days=5)
            if lang == "de":
                subject = f"Online-Test für Ihre Bewerbung als {role}"
                body = (
                    f"Guten Tag,\n\nals nächsten Schritt bitten wir Sie, den Online-Test zu "
                    f"bearbeiten. Bitte schließen Sie den Test bis zum {due.strftime('%d.%m.%Y')} ab.\n\n"
                    f"Den Zugang finden Sie in Ihrem Bewerbungsportal.\n\n"
                    f"Mit freundlichen Grüßen\n{company}"
                )
            else:
                subject = f"Next step: online assessment for {role}"
                body = (
                    f"Hi,\n\nAs a next step we would like you to complete a short online "
                    f"assessment. Please complete the assessment by {due.strftime('%d %B %Y')}.\n\n"
                    f"You can find the link in your candidate portal.\n\nBest,\n{company}"
                )
            message, message_id = _msg(
                subject=subject, body=body, from_name=f"{company} Recruiting",
                from_addr=f"recruiting@{domain}", to_addr=owner, when=when, rng=rng, references=list(thread),
            )
            thread.append(message_id)
            emit(message, message_id, "assessment", True, company, role, lang,
                 deadline=due.date().isoformat())

        # 3. terminal event
        if outcome > 0.78:
            when = applied + timedelta(days=rng.randint(8, 25))
            slot = when + timedelta(days=rng.randint(3, 12))
            if lang == "de":
                subject = f"Einladung zum Vorstellungsgespräch – {role}"
                body = (
                    f"Sehr geehrte Bewerberin, sehr geehrter Bewerber,\n\n"
                    f"wir würden Sie gerne zu einem Vorstellungsgespräch einladen. "
                    f"Wir schlagen den {slot.strftime('%d. %B %Y')} um 14:30 Uhr vor.\n\n"
                    f"Bitte bestätigen Sie den Termin.\n\nMit freundlichen Grüßen\n"
                    f"Anna Bergmann, {company}"
                )
                contact = "Anna Bergmann"
            else:
                subject = f"Interview invitation – {role}"
                body = (
                    f"Hello,\n\nWe would like to invite you to an interview for the {role} "
                    f"position. We propose {slot.strftime('%d %B %Y')} at 10:00 CET.\n\n"
                    f"Please confirm whether this time works for you.\n\nBest regards,\n"
                    f"Sarah Klein, {company}"
                )
                contact = "Sarah Klein"
            message, message_id = _msg(
                subject=subject, body=body, from_name=contact,
                from_addr=f"{contact.split()[0].lower()}@{domain}", to_addr=owner,
                when=when, rng=rng, references=list(thread),
            )
            thread.append(message_id)
            hour = 14 if lang == "de" else 10
            minute = 30 if lang == "de" else 0
            emit(message, message_id, "interview_invite", True, company, role, lang,
                 event=slot.replace(hour=hour, minute=minute, second=0,
                                    microsecond=0).isoformat())
        elif outcome > 0.35:
            when = applied + timedelta(days=rng.randint(10, 40))
            if lang == "de":
                subject = f"Ihre Bewerbung als {role}"
                body = (
                    f"Sehr geehrte Bewerberin, sehr geehrter Bewerber,\n\n"
                    f"vielen Dank für Ihr Interesse an {company}. Leider müssen wir Ihnen "
                    f"mitteilen, dass wir Ihre Bewerbung nicht weiter berücksichtigen können.\n\n"
                    f"Wir wünschen Ihnen für Ihren weiteren Werdegang alles Gute.\n\n"
                    f"Mit freundlichen Grüßen\n{company}"
                )
            else:
                subject = f"Update on your application – {role}"
                body = (
                    f"Hello,\n\nThank you for your interest in {company}. After careful "
                    f"consideration we have decided not to move forward with your application "
                    f"for the {role} role.\n\nWe wish you the best in your search.\n\n"
                    f"Kind regards,\n{company} Talent Team"
                )
            message, message_id = _msg(
                subject=subject, body=body, from_name=f"{company} via {ats_name}",
                from_addr=ats_addr, to_addr=owner, when=when, rng=rng, references=list(thread),
            )
            emit(message, message_id, "rejection", False, company, role, lang)
        # else: silence — these become the ghosted cases

    # university admissions thread
    for index, (uni, domain, lang) in enumerate(UNIVERSITIES):
        programme = PROGRAMMES[index % len(PROGRAMMES)]
        when = end - timedelta(days=rng.randint(20, 90))
        due = when + timedelta(days=rng.randint(10, 40))
        subject = f"Your application to {programme}" if lang == "en" else f"Ihre Bewerbung: {programme}"
        body = (
            f"Dear applicant,\n\nYour application for {programme} is incomplete. "
            f"Please upload your certified transcript and language certificate no later than "
            f"{due.strftime('%d %B %Y')}.\n\nAdmissions Office, {uni}"
            if lang == "en"
            else
            f"Sehr geehrte Bewerberin, sehr geehrter Bewerber,\n\n"
            f"Ihre Bewerbung für {programme} ist unvollständig. Bitte reichen Sie noch die "
            f"beglaubigte Übersetzung Ihrer Zeugnisse bis zum {due.strftime('%d.%m.%Y')} nach.\n\n"
            f"Studierendensekretariat, {uni}"
        )
        message, message_id = _msg(
            subject=subject, body=body, from_name=f"{uni} Admissions",
            from_addr=f"admissions@{domain}", to_addr=owner, when=when, rng=rng,
        )
        emit(message, message_id, "document_request", True, uni, programme, lang,
             deadline=due.date().isoformat())

    # noise: newsletters, alerts, security notices, one real human
    for index in range(noise):
        when = end - timedelta(days=rng.randint(0, 90), hours=rng.randint(0, 23))
        kind = index % 4
        if kind == 0:
            message, message_id = _msg(
                subject="12 new jobs match your saved search",
                body=(
                    "New jobs for you this week.\n\nData Engineer at various companies.\n\n"
                    "Unsubscribe from these alerts at any time."
                ),
                from_name="LinkedIn Job Alerts", from_addr="jobalerts-noreply@linkedin.com",
                to_addr=owner, when=when, rng=rng,
                extra_headers={
                    "List-Unsubscribe": "<https://linkedin.example/unsub>",
                    "Precedence": "bulk",
                },
            )
            emit(message, message_id, "job_alert", False, "LinkedIn", "", "en")
        elif kind == 1:
            message, message_id = _msg(
                subject="Unser Newsletter: Neuigkeiten im September",
                body=(
                    "Sehr geehrte Kundin, sehr geehrter Kunde,\n\nhier sind unsere Neuigkeiten "
                    "des Monats.\n\nSie können den Newsletter jederzeit abbestellen."
                ),
                from_name="Beispiel Shop", from_addr="newsletter@beispielshop.de",
                to_addr=owner, when=when, rng=rng,
                extra_headers={
                    "List-Unsubscribe": "<https://beispielshop.de/unsub>",
                    "List-Id": "<news.beispielshop.de>",
                },
            )
            emit(message, message_id, "newsletter", False, "Beispiel Shop", "", "de")
        elif kind == 2:
            message, message_id = _msg(
                subject="Security alert: new sign-in",
                body=(
                    "A new sign-in to your account was detected from a new device.\n"
                    "If this was not you, please secure your account."
                ),
                from_name="Account Security", from_addr="no-reply@accounts.example.com",
                to_addr=owner, when=when, rng=rng,
                extra_headers={"Auto-Submitted": "auto-generated"},
            )
            emit(message, message_id, "admin", False, "Accounts", "", "en")
        else:
            message, message_id = _msg(
                subject="Kaffee am Freitag?",
                body=(
                    "Hallo,\n\nschön, dich letzte Woche getroffen zu haben. Hast du am Freitag "
                    "Zeit für einen Kaffee?\n\nViele Grüße\nJonas"
                ),
                from_name="Jonas Weber", from_addr="jonas.weber@gmx.de",
                to_addr=owner, when=when, rng=rng,
            )
            emit(message, message_id, "personal", True, "", "", "de")

    order = sorted(range(len(messages)), key=lambda i: messages[i]["Date"])
    return [messages[i] for i in order], [labels[i] for i in order]


def write_mbox(
    directory: str | Path,
    *,
    seed: int = 7,
    applications: int = 24,
    noise: int = 40,
    owner: str = "you@example.com",
) -> tuple[Path, Path]:
    """Write `sample.mbox` plus `sample.labels.jsonl` and return both paths."""
    directory = Path(directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    messages, labels = generate_corpus(
        seed=seed, applications=applications, noise=noise, owner=owner
    )

    mbox_path = directory / "sample.mbox"
    with mbox_path.open("w", encoding="utf-8") as handle:
        for message in messages:
            handle.write(f"From sift@example {message['Date']}\n")
            for line in str(message).split("\n"):
                # mbox 'From ' quoting: a body line starting with From must be escaped
                handle.write((">" + line if line.startswith("From ") else line) + "\n")
            handle.write("\n")

    labels_path = directory / "sample.labels.jsonl"
    with labels_path.open("w", encoding="utf-8") as handle:
        for label in labels:
            handle.write(
                json.dumps(
                    {
                        "message_id": label.message_id,
                        "category": label.category,
                        "action_required": label.action_required,
                        "org": label.org,
                        "role": label.role,
                        "language": label.language,
                        **({"deadline": label.deadline} if label.deadline else {}),
                        **({"event_datetime": label.event_datetime} if label.event_datetime else {}),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return mbox_path, labels_path
