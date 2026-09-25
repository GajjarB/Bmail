"""A job seeker's real inbox, in miniature.

Modelled on the mix a real mailbox holds, not on the patterns the rules were
written from: job boards and alerts in two languages, a handful of genuine
application replies, and a large majority of mail that has nothing to do with
applications at all but uses the same words. A credit card "application" is
declined "unfortunately"; a fund confirms "receipt of your application"; a DSL
order says "leider"; a hiring ad promises "no interview".

Every message is invented: names, numbers and addresses are made up. Sender
domains are the real public ones, because that is what the rules see.

Each entry carries what a person would file it as, and whether it needs them.
`expect` is a set where two answers are equally right (a platform's "invitation
to apply" is outreach or an alert, depending on how you read it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage

ME = "alex.demo@gmail.example"

PREHEADER = '<div style="display:none">{text} ' + "&zwnj;&nbsp;&#847;" * 40 + "</div>"

BULK = {
    "List-Unsubscribe": "<https://unsubscribe.example/u?id=1>",
    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    "Precedence": "bulk",
}


@dataclass
class Case:
    name: str
    sender: str
    subject: str
    body: str
    expect: set[str]
    needs_you: bool | None            # None: either answer is defensible
    html: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    job: bool = False                 # about an application or a job search at all

    def raw(self, index: int) -> bytes:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = ME
        message["Subject"] = self.subject
        message["Date"] = f"Thu, {1 + index % 27:02d} Sep 2026 {8 + index % 10:02d}:{index % 60:02d}:00 +0200"
        message["Message-ID"] = f"<{self.name}.{index}@realworld.example>"
        for key, value in self.headers.items():
            message[key] = value
        if self.html:
            message.set_content("This message is best viewed in HTML.")
            message.add_alternative(self.body, subtype="html")
        else:
            message.set_content(self.body)
        return bytes(message)


def page(preheader: str, *blocks: str, footer: str = "") -> str:
    cells = "".join(f"<tr><td>{block}</td></tr>" for block in blocks)
    return (
        f"<html><body>{PREHEADER.format(text=preheader)}"
        f'<table width="100%"><tr><td><a href="https://x.example/web">View in browser</a></td></tr>'
        f"{cells}<tr><td style='font-size:11px'>{footer}</td></tr></table></body></html>"
    )


UNSUB_EN = "You are receiving this email because you signed up. <a href='#'>Unsubscribe</a> · <a href='#'>Privacy</a>"
UNSUB_DE = "Sie erhalten diese E-Mail, weil Sie sich angemeldet haben. <a href='#'>Abmelden</a> · <a href='#'>Datenschutz</a>"

CASES: list[Case] = [
    # --- genuine application mail -------------------------------------------
    Case("amazon-waitlist", "Amazon Jobs <noreply@jobs.amazon.com>", "Hello from Amazon",
         "Hello,\n\nThank you for your application at Amazon. Unfortunately, it is not yet possible "
         "for us to give you a specific date as to when there will be a vacancy. Therefore we ask "
         "for your patience. We will contact you as soon as a position becomes available.\n\n"
         "Best regards,\nYour Amazon Recruiting Team",
         {"acknowledgement"}, False, job=True),
    Case("amazon-in-process", "Amazon Jobs <noreply@jobs.amazon.com>",
         "Your Application - Amazon Sortation Associate (m/f/d) - Augsburg",
         "Hello Alex Demo,\n\nWe would like to thank you again for the time you have spent in our "
         "application process for the position Amazon Sortation Associate (m/f/d) - Augsburg at Amazon. "
         "We will contact you again as soon as we have news about your application.\n\n"
         "Kind regards,\nAmazon Recruiting",
         {"acknowledgement"}, False, job=True),
    Case("personio-ack", "Weser Data GmbH <jobs@weser-data.jobs.personio.de>",
         "Ihre Bewerbung als Backend Developer",
         "Hallo Alex,\n\nvielen Dank für Ihre Bewerbung als Backend Developer. Wir haben Ihre "
         "Unterlagen erhalten und melden uns in den nächsten zwei Wochen bei Ihnen.\n\n"
         "Viele Grüße\nIhr Weser Data Recruiting Team",
         {"acknowledgement"}, False, job=True),
    Case("greenhouse-reject", "Lumen Analytics <no-reply@us.greenhouse-mail.io>",
         "Your application to Lumen Analytics",
         "Hi Alex,\n\nThank you for your interest in the Data Engineer position. After careful "
         "consideration, we have decided not to move forward with your application at this time.\n\n"
         "We wish you all the best.\nLumen Analytics Talent Team",
         {"rejection"}, False, job=True),
    Case("de-reject", "Kranich Mobility <karriere@kranich-mobility.example>",
         "Ihre Bewerbung als Platform Engineer",
         "Sehr geehrter Herr Demo,\n\nvielen Dank für Ihr Interesse an unserem Unternehmen. Leider "
         "müssen wir Ihnen mitteilen, dass wir Ihre Bewerbung nicht weiter berücksichtigen können.\n\n"
         "Mit freundlichen Grüßen\nKranich Mobility HR",
         {"rejection"}, False, job=True),
    Case("interview-person", "Jana Schmidt <jana.schmidt@nordlicht-labs.example>",
         "Interview - Android Developer",
         "Hi Alex,\n\nthanks for your application. We'd like to invite you to an interview with our "
         "mobile team. Would Tuesday, 6 October at 10:00 work for you?\n\nBest,\nJana",
         {"interview_invite"}, True, job=True),
    Case("recruiter-person", "Tom Becker <tom.becker@talentbridge.example>",
         "Android role in Munich",
         "Hi Alex,\n\nI came across your profile and think you would be a great fit for a senior "
         "Android role one of our clients in Munich is hiring for. Would you be open to a short call "
         "this week?\n\nCheers,\nTom",
         {"recruiter_outreach"}, True, job=True),
    Case("platform-invite", "Taskforce AI <team@taskforce.example>", "Invitation to Apply: Bilingual German Writer",
         "Hi Alex,\n\nWe've identified you as a great fit for the Bilingual Writer - German role. In "
         "this project, you'll use your native German language skills to help evaluate and improve "
         "AI models. Apply here: https://taskforce.example/jobs/123\n\nThe Taskforce Team",
         {"recruiter_outreach", "job_alert"}, None, job=True),
    Case("platform-reminder", "Taskforce AI <team@taskforce.example>", "Reminder to Apply: German Writer Role",
         "Hi Alex,\n\nWe recently reached out because we identified you as a strong fit for our "
         "Bilingual Writer - German (Germany) opportunity. Just wanted to send a quick reminder in "
         "case you missed our last email.\n\nThe Taskforce Team",
         {"recruiter_outreach", "job_alert"}, None, headers=BULK, job=True),

    # --- job boards and alerts -----------------------------------------------
    Case("indeed-alert", "Indeed <donotreply@jobalert.indeed.com>",
         "Contact Team Technician bei V2X. 14 weitere Jobs als english speaking germany",
         page("Overview: This position description is subject to change",
              "<b>Contact Team Technician</b><br>V2X · Stuttgart", "<b>IT Support Specialist</b><br>Kestrel IT · Augsburg",
              "<a href='#'>Alle Jobs ansehen</a>", footer=UNSUB_DE),
         {"job_alert"}, False, html=True, headers=BULK, job=True),
    Case("indeed-match", "Indeed <donotreply@match.indeed.com>",
         "Trainee IT & Artificial Intelligence (f/m/x) @ BMW",
         page("It looks like your background could be a match",
              "Hi ALEX, It looks like your background could be a match for this Trainee IT & Artificial "
              "Intelligence (f/m/x) role. Please submit a quick application if you have any interest.",
              "<a href='#'>Apply now</a>", footer=UNSUB_EN),
         {"job_alert"}, False, html=True, headers=BULK, job=True),
    Case("linkedin-alert", "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
         "App-Entwickler (m/w/d) - Mobile Payment at petaFuel GmbH",
         page("petaFuel GmbH App-Entwickler (m/w/d) - Mobile Payment",
              "<b>App-Entwickler (m/w/d) - Mobile Payment</b><br>petaFuel GmbH · Freising",
              "<b>Android Developer</b><br>Brightfield AI · Berlin", "<a href='#'>See all jobs</a>",
              footer=UNSUB_EN),
         {"job_alert"}, False, html=True, headers=BULK, job=True),
    Case("linkedin-jobs", "LinkedIn <jobs-noreply@linkedin.com>", "SkillsCapital is hiring for a Java role",
         page("Discover roles that match your interests", "<b>Java Developer</b><br>SkillsCapital · Remote",
              footer=UNSUB_EN),
         {"job_alert"}, False, html=True, headers=BULK, job=True),
    Case("stepstone-rec", "StepStone <info@jobagent.stepstone.de>",
         "Alex, our recommendation: Senior Softwareentwickler - Schwerpunkt Frontend (m/w/d)",
         page("Senior Softwareentwickler - Schwerpunkt Frontend",
              "<b>Senior Softwareentwickler (m/w/d)</b><br>adesso SE · Stuttgart", "<a href='#'>Jetzt bewerben</a>",
              footer=UNSUB_DE),
         {"job_alert"}, False, html=True, headers=BULK, job=True),
    Case("stepstone-companies", "StepStone <info@jobagent.stepstone.de>",
         "adesso SE and 4 other companies are looking for candidates like you",
         page("Don't miss out on your chance to find the right job!",
              "adesso SE, Tessera GmbH and 3 more are looking for candidates like you.", footer=UNSUB_EN),
         {"job_alert"}, False, html=True, headers=BULK, job=True),
    Case("xing-jobs", "XING <jobs@mail.xing.com>",
         "NewTec GmbH, Airbus and other great companies are looking for candidates just like you!",
         page("New job recommendations", "Hi Alex, We've got new job recommendations for you.",
              "<b>Büroleitung / Quereinsteiger (w/m/d)</b> Urgently hiring", footer=UNSUB_EN),
         {"job_alert"}, False, html=True, headers=BULK, job=True),
    Case("sap-agent", "SAP <sap-jobnotification@noreply12.jobs2web.com>", "New SAP Jobs Posted",
         "The following jobs matched your search agent at SAP and can be found at jobs.sap.com.\n\n"
         "Agent: Working Student (f/m/d) - AI Engineer Intern, Walldorf DE\n\nJob Matches:\n"
         "Working Student (f/m/d) - AI Engineer, Walldorf\n",
         {"job_alert"}, False, headers=BULK, job=True),
    Case("agency-roundmail", "Jutta Berger <jberger@familienagentur.example>",
         "neues Stellenangebot: Nanny, Familienmanagerin (m,w,d) in Teilzeit bei Augsburg",
         "Lieber Herr Demo,\n\nDie Familienagentur hat wieder ein neues Stellenangebot zu vergeben. Bei dieser "
         "E-Mail handelt es sich um eine Rundmail an unseren gesamten Bewerberpool. Falls die Stelle "
         "für Sie interessant ist, melden Sie sich gerne.\n\nViele Grüße\nJutta Berger",
         {"job_alert", "newsletter"}, False, job=True),
    Case("amazon-hiring-ad", "Amazon <noreply@de.hiring.amazon>",
         "Apply to Amazon now - No CV. No interview. No problem.",
         page("A great-paying job is just a few clicks away.",
              "Apply in minutes: no CV, no interview. Start as soon as next week.", footer=UNSUB_EN),
         {"job_alert", "newsletter"}, False, html=True, headers=BULK, job=True),

    # --- career content, social ----------------------------------------------
    Case("linkedin-post", "LinkedIn <updates-noreply@linkedin.com>",
         "Sam Carter and others share their thoughts on LinkedIn",
         page("Had a 2nd round interview. Right after it, same day, no prep time",
              "Had a 2nd round interview. Right after it, same day, no prep time, they gave me a case study.",
              footer=UNSUB_EN),
         {"newsletter"}, False, html=True, headers=BULK),
    Case("linkedin-pymk", "LinkedIn <messages-noreply@linkedin.com>",
         "Someone at a university hospital you may know",
         page("Alex, we'd like you to meet Jonas Keller!", "Jonas Keller · Research Scientist",
              footer=UNSUB_EN),
         {"newsletter"}, False, html=True, headers=BULK),
    Case("linkedin-event", "LinkedIn <linkedin@em.linkedin.com>",
         "You're invited: Job search strategies for the current job market.",
         page("Join the next Career Talks event to hear tips from the experts.",
              "You're invited to our live Career Talks event on Thursday.", footer=UNSUB_EN),
         {"newsletter"}, False, html=True, headers=BULK),
    Case("jobscan-news", "Jobscan <support@jobscan.co>", "HireReady Vol. 103: I Stopped Counting My Applications",
         page("Job Seeker Story: What changed when I focused less on applications",
              "Smarter ways to optimize your resume, write a cover letter, and practice interviews.",
              footer=UNSUB_EN),
         {"newsletter"}, False, html=True, headers=BULK),

    # --- developer notifications ---------------------------------------------
    Case("github-comment", "google-labs-jules[bot] <notifications@github.com>",
         "Re: [demo/notes-app] Fix intent URL validation bypass (PR #447)",
         "google-labs-jules[bot] left a comment (demo/notes-app#447)\n\nJules, reporting for duty! "
         "I'm here to lend a hand with this pull request.\n\n--\nReply to this email directly, view it "
         "on GitHub: https://github.com/demo/notes-app/pull/447\nYou are receiving this because you "
         "authored the thread.",
         {"other", "admin", "newsletter"}, False,
         headers={"List-ID": "demo/notes-app <notes-app.demo.github.com>",
                  "List-Unsubscribe": "<https://github.com/notifications/unsubscribe/x>",
                  "X-GitHub-Reason": "author", "Precedence": "list"}),
    Case("jules-digest", "Jules <jules-noreply@google.com>", "Jules found improvements for your repos",
         "Hi, Jules has proactively identified a few opportunities to improve your code. Check out the "
         "suggestions below:\n\ndemo/budget-app: Commented-out dispatchTouchEvent method",
         {"other", "admin", "newsletter"}, False, headers=BULK),

    # --- accounts and security -----------------------------------------------
    Case("google-security", "Google <no-reply@accounts.google.com>", "Security alert",
         "App password created to sign in to your account alex.demo@gmail.example\n\nIf you didn't "
         "generate this password for Bmail, someone might be using your account. Check and secure "
         "your account now.\n\nYou received this email to let you know about important changes to "
         "your Google Account and services.",
         {"admin"}, False),
    Case("canva-code", "Canva <no-reply@account.canva.com>", "Ready to make changes to your Canva account?",
         page("Enter the code below to make changes to your account",
              "Your verification code is <b>482 913</b>. It expires in 10 minutes."),
         {"admin"}, False, html=True),
    Case("canva-passkey", "Canva <no-reply@account.canva.com>", "Passkey authentication added to your Canva account",
         page("This change is for the Canva account of alex.demo@gmail.example",
              "A passkey was added to your account. If this wasn't you, reset your password."),
         {"admin"}, False, html=True),
    Case("google-shared", "Google <noreply-accounts@google.com>", "You shared some Google Account data with Canva",
         "Keep track of your Google Account data\n\nYou're receiving this email because you used Sign "
         "in with Google to sign in to Canva on September 24.",
         {"admin"}, False),

    # --- banks, money, forms ---------------------------------------------------
    Case("bank-document", "Beispielbank <info-noreply@beispielbank.example>", "Neues Dokument / New document",
         "Sehr geehrte(r) Herr Demo,\n\nSie haben ein neues Dokument in Ihrem Beispielbank Postfach. "
         "Melden Sie sich einfach im Online Banking an, um das Dokument abzurufen.\n\nMit freundlichen "
         "Grüßen\nIhre Beispielbank",
         {"admin"}, False),
    Case("fund-confirmation", "Example Mutual Fund <enq_o@fundservices.example>",
         "Transaction Confirmation - Subscription under Folio No. 11111111/01",
         "Dear Investor,\n\nThank you for your investment with Example Mutual Fund! We acknowledge the "
         "receipt of your subscription in the Folio No: 11111111/01. We are glad to confirm that the "
         "same has been processed.",
         {"admin"}, False),
    Case("fund-application", "Sample Fund House <enq_p@fundservices.example>",
         "Folio 22222222 - Processing of Purchase in Sample Mutual Fund",
         "Dear Alex Demo,\n\nGreetings from Sample Mutual Fund! We confirm that your "
         "investment application under your folio is processed in Sample Multi Cap Fund - Growth.",
         {"admin"}, False),
    Case("credit-card-declined", "Example Bank <alerts@examplebank.example>", "Your Credit Card Application",
         "13-08-2026\n\nDear Customer,\n\nWe have reviewed your application no. C1-00000000000000-C1, "
         "however, we regret to inform you that we are unable to process your Credit Card application "
         "as it does not meet the internal policy criteria.",
         {"admin"}, False),
    Case("card-marketing", "CardCompare <support@cardcompare.example>", "Hurry, complete your application now.",
         page("to lock in your special Credit Card offer",
              "Hi Alex, We're still holding the Example Rewards Credit Card for you. Act fast & complete your "
              "application in less than 3 mins!", footer=UNSUB_EN),
         {"newsletter"}, False, html=True, headers=BULK),
    Case("gov-form-sent", "Online Services <no-reply@services.example-stadt.de>",
         "Your application has been sent",
         "Many thanks, Alex Demo! Your request has been successfully submitted. Your case number is: "
         "2026-08-13-XX-1. The competent authority will now check whether all the necessary information "
         "has been provided.",
         {"admin"}, None),
    Case("gov-autoreply", "Bürgeramt <buergeramt@example-stadt.de>",
         "Automatische Antwort: Fallnummer 2026-08-13-XX-1 - Frage zur Bearbeitungsgebühr",
         "Sehr geehrte Damen und Herren,\n\nIhre Nachricht ist beim Bürgeramt eingegangen. "
         "Aufgrund der gestiegenen Anzahl an Anfragen kommt es zu längeren Bearbeitungszeiten.",
         {"admin"}, False, headers={"Auto-Submitted": "auto-replied"}),

    # --- orders, travel, deliveries -------------------------------------------
    Case("dsl-patience", "NetzPlus <auftragsmanagement@netzplus.example>", "Ihr Internetanschluss - wir bitten Sie um etwas Geduld",
         "Kundennummer: 000000000\n\nGuten Tag Alex Demo,\n\nden genauen Termin für die Schaltung Ihres "
         "Tarifs NetzPlus DSL 50 kann ich Ihnen leider noch nicht nennen. Es tut mir leid, dass Sie noch "
         "warten müssen.",
         {"admin"}, False),
    Case("dsl-complete-order", "NetzPlus <support@netzplus.example>", "Bitte schließen Sie Ihre NetzPlus Bestellung ab",
         "Ihre NetzPlus Bestellnummer: XXXX0000\n\nGuten Tag Alex Demo,\n\nwie vereinbart, haben wir Ihren "
         "NetzPlus DSL-Auftrag erfasst. Wichtig: Schließen Sie jetzt Ihren Auftrag ab.",
         {"admin"}, None),
    Case("dsl-received", "NetzPlus <auftragsvorbereitung@netzplus.example>", "Ihr NetzPlus Auftrag ist eingegangen",
         "Guten Tag Alex Demo,\n\nvielen Dank für Ihre Bestellung. Wir freuen uns, dass Sie sich für NetzPlus "
         "entschieden haben!",
         {"admin"}, False),
    Case("dsl-cancelled", "NetzPlus <support@netzplus.example>", "Wir haben Ihren Auftrag storniert",
         "Guten Tag Alex Demo,\n\nvielen Dank für Ihr Interesse an einem DSL-Anschluss bei NetzPlus. Die "
         "Verfügbarkeitsprüfung war leider nicht erfolgreich, deshalb haben wir Ihren Auftrag storniert.",
         {"admin"}, False),
    Case("parcel", "DHL <noreply@dhl.de>", "Ihre Zalando Sendung befindet sich auf dem Weg in eine Filiale",
         page("Wichtige Information zu Ihrer Sendung", "Ihre Sendung 00340000000000000000 wird in eine Filiale gebracht."),
         {"admin"}, False, html=True),
    Case("bus-change", "FlixBus <noreply@me.flixbus.com>", "Your station address changed.",
         "Dear Flix guest,\n\nthank you very much for your booking 3370000000 regarding the ride from "
         "Munich to Prague on 14.08.2026 13:00. Unfortunately, the bus stop has changed.",
         {"admin"}, None),
    Case("transit-receipt", "Transports Exemple <ne-pas-repondre@titres.transports-exemple.fr>",
         "Application Transports Exemple Votre justificatif d'achat / Purchase Receipt",
         "Bonjour Alex Demo\n\nVous avez effectué un achat le 14 septembre 2026 à 13h23 sur Transports "
         "Exemple. Cet email tient lieu de justificatif d'achat.",
         {"admin"}, False),
    Case("scheme-notice", "Example Motors <scheme2026@registrar.example>",
         "Example Motors Limited | Company Scheme Application No. CA (CAA) 113/MB/2026",
         "EXAMPLE MOTORS LIMITED\nRegistered Office: 1 Example Street, Mumbai.\n\nNotice of the meeting of "
         "equity shareholders in respect of the Company Scheme Application.",
         {"admin", "newsletter"}, False),

    # --- shops, media, newsletters --------------------------------------------
    Case("fashion-promo", "Zalando Outlet <hello@info.zalando-outlet.de>",
         "Exklusiv für Member: Sichere dir 20% Rabatt auf Oberteile",
         page("Mach deinen Kleiderschrank bereit für den Herbst.", "20% Rabatt auf Oberteile, nur bis Sonntag.",
              footer=UNSUB_DE),
         {"newsletter"}, False, html=True, headers=BULK),
    Case("sale-promo", "AJIO <alert@mailer.ajio.in>", "18 Hours Only. Ready?",
         page("one8 on AJIO. 27th Sep. 8AM. Wishlist Now.", "The best of the best, lined up for you!",
              footer=UNSUB_EN),
         {"newsletter"}, False, html=True, headers=BULK),
    Case("cinema", "Cineplex <news@cineplex.de>", "Kinoprogramm 24.09. - 30.09. | Avengers zurück auf der Leinwand",
         page("Überleben beginnt mit Vertrauen", "Hallo Alex, diese Filme erwarten Dich diese Woche!",
              footer=UNSUB_DE),
         {"newsletter"}, False, html=True, headers=BULK),
    Case("pinterest", "Pinterest <recommendations@discover.pinterest.com>", "App Dashboard UI mood",
         page("Appointment UI, Cycle Calendar Screen", "Ideas picked for you", footer=UNSUB_EN),
         {"newsletter"}, False, html=True, headers=BULK),
    Case("course-promo", "Coursera <Coursera@m.learn.coursera.org>", "New from Microsoft: 15+ tech courses",
         page("Build your career path with programs in AI, cloud, data, and more.",
              "Enroll today and start learning.", footer=UNSUB_EN),
         {"newsletter"}, False, html=True, headers=BULK),
    Case("ai-newsletter", "The Batch <thebatch@deeplearning.ai>",
         "What Stage Is Your Project? Takeaways For AI Engineering",
         page("Frontier models, classifiers, running two agents at once",
              "Dear friends, in this issue: an interview with a founder on shipping agents.", footer=UNSUB_EN),
         {"newsletter"}, False, html=True, headers=BULK),
    Case("grad-school", "Northfield Admissions <gradadmissions@northfield.example.edu>",
         "Alex, a master's degree will unlock new opportunities in your career.",
         page("Join us virtually this September", "Register for our Graduate Virtual Information Session!",
              footer=UNSUB_EN),
         {"newsletter"}, False, html=True, headers=BULK),
    Case("kaggle", "Kaggle <no-reply@kaggle.com>", "Competition Launch: The Developer Agent Competition",
         "Hi Alex Demo,\n\nToday's best coding agents rely on massive API models running in the cloud. "
         "Join the new competition.\n\nUnsubscribe from Kaggle emails.",
         {"newsletter"}, False, headers=BULK),
    Case("broker-digest", "Market Digest <noreply@digest.brokerapp.example>",
         "Govt cuts edible oil import duty, & more - Market Digest",
         page("All you need to know about the day", "Sensex 73580.54 ▼ 1.67%", footer=UNSUB_EN),
         {"newsletter"}, False, html=True, headers=BULK),
    Case("citation", "Academia.edu <premium@academia-mail.com>", "J. Novak cited your paper",
         page("View your citation", "J. Novak cited your paper.", footer=UNSUB_EN),
         {"newsletter", "other"}, False, html=True, headers=BULK),
    Case("cloud-credits", "Google Developer Program <googledev-noreply@google.com>",
         "You've unlocked monthly Google Cloud credits",
         page("Claim your credits", "You get monthly Google Cloud credits to prototype and test.", footer=UNSUB_EN),
         {"newsletter", "admin"}, False, html=True, headers=BULK),
    Case("community", "Youth Forum <ym@community.example.org>", "A special volunteering opportunity this weekend",
         page("View this email in your browser", "Dear members, we are pleased to inform you about our event.",
              footer=UNSUB_EN),
         {"newsletter"}, False, html=True, headers=BULK),

    # --- people ------------------------------------------------------------------
    Case("friend", "Priya Nair <priya.nair@gmail.example>", "Saturday?",
         "Hey Alex,\n\nare we still on for dinner on Saturday? Let me know by Friday so I can book.\n\nPriya",
         {"personal"}, True),
    Case("landlord", "Martin Weber <m.weber@web.example>", "Wohnungsübergabe",
         "Hallo Herr Demo,\n\nkönnen wir die Wohnungsübergabe auf Montag um 18 Uhr verschieben? Bitte "
         "geben Sie mir kurz Bescheid.\n\nViele Grüße\nMartin Weber",
         {"personal"}, True),
]


# Written after the rules above were tuned, and measured once before anything
# was changed for it: the honest estimate of how Bmail does on mail it was not
# fitted to.
HOLDOUT: list[Case] = [
    Case("smartrecruiters-ack", "Tessera GmbH <noreply@smartrecruiters.com>",
         "Thank you for applying to Tessera GmbH",
         "Hi Alex,\n\nThank you for applying for the Backend Engineer position at Tessera GmbH. "
         "Our team will review your application and get back to you.\n\nTessera Talent Acquisition",
         {"acknowledgement"}, False, job=True),
    Case("de-ack-company", "Stadtwerke Beispiel <bewerbung@stadtwerke-beispiel.example>",
         "Eingangsbestätigung Ihrer Bewerbung",
         "Sehr geehrter Herr Demo,\n\nhiermit bestätigen wir den Eingang Ihrer Bewerbung als "
         "IT-Administrator. Wir melden uns nach Abschluss der Bewerbungsfrist.\n\nIhre Personalabteilung",
         {"acknowledgement"}, False, job=True),
    Case("lever-reject", "Brightfield AI <no-reply@hire.lever.co>", "Your application at Brightfield AI",
         "Hi Alex,\n\nThanks for your interest in the Machine Learning Engineer role. We have decided "
         "to move forward with other candidates whose experience more closely matches our needs.\n\n"
         "Best,\nBrightfield AI Recruiting",
         {"rejection"}, False, job=True),
    Case("de-interview", "Katrin Vogel <k.vogel@helix-payments.example>", "Ihre Bewerbung - nächster Schritt",
         "Hallo Herr Demo,\n\nvielen Dank für Ihre Bewerbung. Wir würden Sie gerne in einem ersten "
         "Gespräch kennenlernen. Hätten Sie am Donnerstag um 11 Uhr Zeit?\n\nViele Grüße\nKatrin Vogel",
         {"interview_invite", "scheduling"}, True, job=True),
    Case("hackerrank", "Kestrel IT <support@hackerrank.com>", "Kestrel IT has invited you to a coding test",
         "Hi Alex,\n\nKestrel IT has invited you to take a coding challenge for the Software Engineer "
         "role. Please complete the test within 5 days.",
         {"assessment"}, True, job=True),
    Case("offer", "Nina Hartmann <nina.hartmann@tessera.example>", "Offer - Backend Engineer",
         "Hi Alex,\n\nwe are delighted to offer you the position of Backend Engineer. Please find "
         "your employment contract attached and let us know by Friday.\n\nNina",
         {"offer"}, True, job=True),
    Case("arbeitsagentur", "Bundesagentur für Arbeit <noreply@arbeitsagentur.de>",
         "Neue Stellenangebote für Ihren Suchauftrag",
         "Guten Tag,\n\nfür Ihren Suchauftrag 'Softwareentwickler Augsburg' gibt es 6 neue Stellenangebote.",
         {"job_alert"}, False, headers=BULK, job=True),
    Case("glassdoor", "Glassdoor Jobs <noreply@glassdoor.com>", "Android Developer jobs near Augsburg",
         page("12 new jobs for you", "<b>Android Developer</b> · Neu-Augsburg", footer=UNSUB_EN),
         {"job_alert"}, False, html=True, headers=BULK, job=True),
    Case("linkedin-inmail", "LinkedIn <messages-noreply@linkedin.com>", "Sarah sent you a new message",
         page("Sarah Lange, Technical Recruiter", "Hi Alex, I have an Android role that might interest you.",
              footer=UNSUB_EN),
         {"recruiter_outreach", "job_alert", "newsletter"}, None, html=True, headers=BULK, job=True),

    Case("paypal", "PayPal <service@paypal.de>", "Sie haben eine Zahlung gesendet",
         "Guten Tag Alex Demo,\n\nSie haben eine Zahlung über 24,99 EUR an Beispiel Shop gesendet. "
         "Transaktionscode: 1AB23456CD789012E",
         {"admin"}, False),
    Case("shop-shipped", "Amazon.de <versandbestaetigung@amazon.de>", "Ihre Amazon.de-Bestellung wurde versandt",
         "Hallo Alex,\n\nIhre Sendung ist unterwegs. Bestellung #302-0000000-0000000. "
         "Voraussichtliche Zustellung: Mittwoch.",
         {"admin"}, False),
    Case("rail-booking", "DB Fernverkehr <buchungsbestaetigung@bahn.de>", "Ihre Buchungsbestätigung",
         "Vielen Dank für Ihre Buchung.\nAuftragsnummer: 000000000000\nAugsburg Hbf - München Hbf, 02.10.2026",
         {"admin"}, False),
    Case("phone-invoice", "Vodafone <rechnung@vodafone.de>", "Ihre Vodafone Rechnung ist da",
         "Hallo Alex Demo,\n\nIhre Rechnung für September steht zum Download bereit. Betrag: 29,99 EUR.",
         {"admin"}, False),
    Case("ms-signin", "Microsoft account team <account-security-noreply@accountprotection.microsoft.com>",
         "Unusual sign-in activity",
         "We detected something unusual about a recent sign-in to the Microsoft account a*****@gmail.example. "
         "If this was you, you can safely ignore this email.",
         {"admin"}, False),
    Case("bank-statement", "N26 <no-reply@n26.com>", "Dein Kontoauszug ist verfügbar",
         "Hallo Alex,\n\ndein Kontoauszug für September ist jetzt in der App verfügbar.",
         {"admin"}, False),
    Case("insurance-antrag", "Techniker Krankenkasse <service@tk.de>", "Ihr Antrag ist bei uns eingegangen",
         "Sehr geehrter Herr Demo,\n\nvielen Dank für Ihren Antrag auf Kostenübernahme. Leider können "
         "wir Ihnen noch keinen genauen Termin für unsere Entscheidung nennen.",
         {"admin"}, False),
    Case("visa-appointment", "VFS Global <donotreply@vfshelpline.com>", "Your visa application appointment",
         "Dear Applicant,\n\nYour appointment for the visa application has been confirmed for "
         "08-10-2026 at 09:30. Please bring your passport and the application form.",
         {"admin"}, None),
    Case("calendar-invite", "Google Calendar <calendar-notification@google.com>",
         "Invitation: Team sync @ Tue 6 Oct 2026 10am",
         "Priya Nair has invited you to Team sync on Tuesday, 6 October at 10am. Going? Yes - Maybe - No",
         {"admin", "other", "personal"}, None),
    Case("uber-receipt", "Uber Receipts <noreply@uber.com>", "Your Tuesday evening trip with Uber",
         page("Thanks for riding, Alex", "Total 18.40 EUR", "Trip from Augsburg Hbf to Söflingen"),
         {"admin"}, False, html=True),
    Case("gitlab", "GitLab <gitlab@mg.gitlab.com>", "demo / bmail | Pipeline #123 has failed",
         "Pipeline #123 has failed!\n\nProject: demo / bmail\nBranch: main\nCommit: fix tests",
         {"other", "admin", "newsletter"}, False,
         headers={"List-Id": "demo/bmail <123.project.gitlab.com>", "X-GitLab-Project": "bmail"}),
    Case("medium", "Medium Daily Digest <noreply@medium.com>", "How I landed my first developer job",
         page("Stories for Alex", "How I landed my first developer job after 200 applications", footer=UNSUB_EN),
         {"newsletter"}, False, html=True, headers=BULK),
    Case("supermarket", "Lidl <newsletter@lidl.de>", "Die Angebote der Woche sind da",
         page("Frische Angebote", "Nur diese Woche: 30% auf Obst", footer=UNSUB_DE),
         {"newsletter"}, False, html=True, headers=BULK),
    Case("streaming", "Netflix <info@mailer.netflix.com>", "Neu auf Netflix: diese Woche",
         page("Jetzt ansehen", "Die neuen Serien dieser Woche", footer=UNSUB_DE),
         {"newsletter"}, False, html=True, headers=BULK),

    Case("colleague", "Lukas Braun <lukas.braun@gmail.example>", "Kurze Frage zu morgen",
         "Hi Alex,\n\nkannst du mir bis heute Abend die Folien schicken? Danke!\n\nLukas",
         {"personal"}, True),
    Case("family", "Ravi Demo <ravi.demo@yahoo.example>", "Call tonight?",
         "Hi beta, can you call us tonight after 8? Mom wants to talk about Diwali plans.",
         {"personal"}, True),
]
