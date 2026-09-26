"""Generate the simulated Arcadia CRM.

Deterministic for a given seed, so a report produced today can be reproduced
tomorrow. The interesting part is not the volume -- 1 200 companies is small --
it is the **defects**, which are injected on purpose and counted, so the
documentation can state exactly what the migration is being asked to survive:

* the same company entered two or three times under different spellings;
* countries written six ways, including a typo;
* dates in four formats, one of them ambiguous (03/04 is April or March);
* amounts with currency symbols, thousands separators, and both decimal marks;
* emails that are not emails, phone numbers with extensions;
* names in ALL CAPS, in lower case, with doubled spaces;
* mojibake from a Latin-1 export read as UTF-8;
* rows pointing at companies that no longer exist;
* codes that were never in the reference tables;
* soft-deleted rows that half the legacy reports forgot to filter out.

Every one of those appears in a real migration. A tool tested only against
clean input is a tool that has not been tested.
"""

from __future__ import annotations

import csv
import datetime as dt
import random
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text

from keystone.config import Settings, get_settings
from keystone.db.engine import get_engine
from keystone.generation import vocabulary as vocab
from keystone.logging_config import get_logger

logger = get_logger(__name__)

# The legacy system went live in 2011 and was frozen in early 2026.
EPOCH = dt.date(2011, 1, 1)
FREEZE = dt.date(2026, 1, 31)


@dataclass
class DefectLog:
    """What was injected, counted by kind.

    Written to the reports directory so the dry-run's findings can be checked
    against the truth -- a data-quality report nobody can verify is a report
    nobody should trust.
    """

    counts: Counter[str] = field(default_factory=Counter)

    def record(self, kind: str, n: int = 1) -> None:
        self.counts[kind] += n

    def as_dict(self) -> dict[str, int]:
        return dict(sorted(self.counts.items()))

    @property
    def total(self) -> int:
        return sum(self.counts.values())


# ---------------------------------------------------------------------------
# Defect helpers. Each one takes a clean value and returns a dirty one.
# ---------------------------------------------------------------------------


def _mojibake(value: str) -> str:
    """What a Latin-1 export looks like when read as UTF-8.

    The classic: 'Société' becomes 'SociÃ©tÃ©'. Encoded here rather than
    hard-coded so it applies to whatever accents the value happens to carry.
    """
    return value.encode("utf-8").decode("latin-1", errors="replace")


def _mangle_case(rng: random.Random, value: str) -> str:
    return rng.choice([value.upper(), value.lower(), f"  {value} ", value.replace(" ", "  ")])


def _format_date(rng: random.Random, value: dt.date, *, dirty: bool) -> str:
    if not dirty:
        return value.strftime("%Y-%m-%d")
    return rng.choice(
        [
            value.strftime("%d/%m/%Y"),  # ambiguous with %m/%d/%Y for day <= 12
            value.strftime("%d-%m-%y"),
            value.strftime("%d.%m.%Y"),
            value.strftime("%Y%m%d"),
        ]
    )


def _format_amount(rng: random.Random, value: float, *, dirty: bool) -> str:
    if not dirty:
        return f"{value:.2f}"
    french = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return rng.choice(
        [
            french,  # '12 500,00'
            f"{value:,.2f}",  # '12,500.00'
            f"€{french}",
            f"$ {value:,.2f}",
            f"{value:.0f}",
            f"{french} EUR",
        ]
    )


def _dirty_email(rng: random.Random, clean: str) -> str:
    return rng.choice(
        [
            clean.upper(),
            f" {clean} ",
            clean.replace("@", "(at)"),
            clean.replace("@", ""),
            "n/a",
            "",
            f"{clean};{clean}",  # two addresses in one field
        ]
    )


def _phone(rng: random.Random, *, dirty: bool) -> str:
    digits = f"{rng.randint(1, 5)}{rng.randint(10_000_000, 99_999_999)}"
    clean = f"+33{digits}"
    if not dirty:
        return clean
    return rng.choice(
        [
            f"0{digits}",
            ".".join(f"0{digits}"[i : i + 2] for i in range(0, 10, 2)),
            f"0{digits} poste 42",
            f"+33 (0){digits[0]} {digits[1:3]} {digits[3:5]} {digits[5:7]} {digits[7:9]}",
            "",
        ]
    )


def _spelling_variant(rng: random.Random, name: str) -> str:
    """Another spelling of the same company -- the duplicate that matters.

    These are the variants a human recognises instantly and an exact-match
    join never does, which is precisely why deduplication needs its own rules
    rather than a DISTINCT.
    """
    variants = [
        name.replace(" SA", " S.A."),
        name.replace(" SAS", " S.A.S"),
        name.upper(),
        name.replace("é", "e").replace("è", "e").replace("ç", "c"),
        f"{name} (ex-{name.split()[0]})",
        name.replace(" & Cie", " et Cie"),
    ]
    return rng.choice([v for v in variants if v != name] or [f"{name} "])


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _random_date(rng: random.Random, start: dt.date = EPOCH, end: dt.date = FREEZE) -> dt.date:
    span = (end - start).days
    return start + dt.timedelta(days=rng.randint(0, span))


def _build_users(rng: random.Random) -> list[dict[str, Any]]:
    users: list[dict[str, Any]] = []
    for i in range(1, 19):
        first = rng.choice(vocab.FIRST_NAMES)
        last = rng.choice(vocab.LAST_NAMES)
        # Three users left the company and were never deactivated; they still
        # own records, which is why owner resolution needs a fallback.
        active = "N" if i > 15 else "Y"
        users.append(
            {
                "usr_id": f"U{i:03d}",
                "login": f"{first[0].lower()}{last.lower()}",
                "fullname": f"{first} {last}",
                "email": f"{first[0].lower()}.{last.lower()}@arcadia-demo.invalid",
                "active": active,
            }
        )
    # One user row with no login at all, because somebody created it by hand.
    users.append(
        {
            "usr_id": "U999",
            "login": None,
            "fullname": "COMPTE TECHNIQUE",
            "email": None,
            "active": None,
        }
    )
    return users


def _build_accounts(
    rng: random.Random, settings: Settings, users: list[dict[str, Any]], log: DefectLog
) -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    seq = 0
    n_unique = settings.n_accounts

    for _ in range(n_unique):
        seq += 1
        stem = rng.choice(vocab.COMPANY_STEMS)
        suffix = rng.choice(vocab.COMPANY_SUFFIXES)
        name = f"{stem} {suffix}"
        city, zip_prefix, country = rng.choice(vocab.CITIES)
        created = _random_date(rng)
        updated = _random_date(rng, created, FREEZE)
        dirty = rng.random() < settings.defect_rate

        ind_code = rng.choice(vocab.INDUSTRIES)[0]
        if rng.random() < 0.05:
            ind_code = rng.choice(vocab.UNREGISTERED_INDUSTRIES)
            log.record("account.unregistered_industry_code")

        company = name
        if dirty:
            roll = rng.random()
            if roll < 0.3:
                company = _mangle_case(rng, name)
                log.record("account.case_or_whitespace")
            elif roll < 0.45:
                company = _mojibake(f"{stem} Société {suffix}")
                log.record("account.mojibake")

        record = {
            "custno": f"C-{seq:06d}",
            "company": company,
            "addr1": f"{rng.randint(1, 220)} {rng.choice(vocab.STREETS)}",
            "addr2": rng.choice(["", "", "", "Bâtiment B", "BP 42", "3e étage"]),
            "city": city if not dirty else _mangle_case(rng, city),
            "zip": f"{zip_prefix}{rng.randint(0, 99):02d}"[:5],
            "ctry": country,
            "phone": _phone(rng, dirty=dirty),
            "fax": _phone(rng, dirty=True) if rng.random() < 0.4 else "",
            "vat_no": f"FR{rng.randint(10, 99)}{rng.randint(100_000_000, 999_999_999)}",
            "ind_code": ind_code,
            "owner_id": rng.choice(users)["usr_id"],
            "status": rng.choice(["A", "A", "A", "I", "PROSPECT", None]),
            "del_flag": "N",
            "notes": rng.choice(
                [
                    "",
                    "",
                    "Client historique",
                    "Facturation centralisée",
                    "Ne pas démarcher avant 2027",
                ]
            ),
            "created": _format_date(rng, created, dirty=dirty),
            "updated": _format_date(rng, updated, dirty=dirty),
        }
        if dirty and rng.random() < 0.2:
            record["ctry"] = rng.choice(["Frnace", "", "F", "  FR"])
            log.record("account.country_unmappable")
        if rng.random() < 0.03:
            record["del_flag"] = "Y"
            log.record("account.soft_deleted")
        accounts.append(record)

    # Duplicates: the same company, entered again under another spelling, with
    # its own customer number. Nothing in the source marks them as related.
    originals = list(accounts)
    for original in originals:
        if rng.random() >= settings.duplicate_rate:
            continue
        seq += 1
        clone = dict(original)
        clone["custno"] = f"C-{seq:06d}"
        clone["company"] = _spelling_variant(rng, str(original["company"]))
        clone["vat_no"] = original["vat_no"] if rng.random() < 0.6 else ""
        clone["phone"] = _phone(rng, dirty=True)
        clone["created"] = _format_date(rng, _random_date(rng), dirty=True)
        accounts.append(clone)
        log.record("account.duplicate_spelling")

    return accounts


def _build_contacts(
    rng: random.Random,
    settings: Settings,
    accounts: list[dict[str, Any]],
    log: DefectLog,
) -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    live_accounts = [a for a in accounts if a["del_flag"] != "Y"]

    for i in range(1, settings.n_contacts + 1):
        account = rng.choice(live_accounts)
        first = rng.choice(vocab.FIRST_NAMES)
        last = rng.choice(vocab.LAST_NAMES)
        dirty = rng.random() < settings.defect_rate
        domain = "example.invalid"
        clean_email = f"{first.lower()}.{last.lower()}@{domain}"

        custno = account["custno"]
        if rng.random() < 0.012:
            custno = f"C-9{rng.randint(10000, 99999)}"  # points nowhere
            log.record("contact.orphan_account_reference")

        record = {
            "persno": f"P-{i:06d}",
            "custno": custno,
            "fname": _mangle_case(rng, first) if dirty else first,
            "lname": _mangle_case(rng, last) if dirty else last,
            "title": rng.choice(vocab.JOB_TITLES),
            "email": _dirty_email(rng, clean_email) if dirty else clean_email,
            "phone": _phone(rng, dirty=dirty),
            "mobile": _phone(rng, dirty=True) if rng.random() < 0.5 else "",
            "role_cd": rng.choice(["DEC", "INF", "USR", "", None]),
            "opt_out": rng.choice(["N", "N", "N", "Y", None]),
            "del_flag": "Y" if rng.random() < 0.04 else "N",
            "created": _format_date(rng, _random_date(rng), dirty=dirty),
        }
        if dirty:
            log.record("contact.case_or_whitespace")
            if record["email"] in {"n/a", ""} or "@" not in str(record["email"]):
                log.record("contact.unusable_email")
        if record["del_flag"] == "Y":
            log.record("contact.soft_deleted")
        contacts.append(record)
    return contacts


def _build_deals(
    rng: random.Random,
    settings: Settings,
    accounts: list[dict[str, Any]],
    contacts: list[dict[str, Any]],
    users: list[dict[str, Any]],
    log: DefectLog,
) -> list[dict[str, Any]]:
    deals: list[dict[str, Any]] = []
    live_accounts = [a for a in accounts if a["del_flag"] != "Y"]
    by_account: dict[str, list[dict[str, Any]]] = {}
    for person in contacts:
        by_account.setdefault(str(person["custno"]), []).append(person)

    for i in range(1, settings.n_opportunities + 1):
        account = rng.choice(live_accounts)
        candidates = by_account.get(str(account["custno"]), [])
        contact: dict[str, Any] | None = rng.choice(candidates) if candidates else None
        dirty = rng.random() < settings.defect_rate
        amount = round(rng.lognormvariate(9.2, 1.0), 2)
        created = _random_date(rng)
        close = created + dt.timedelta(days=rng.randint(5, 400))

        stage = rng.choice(vocab.STAGES)[0]
        if rng.random() < 0.04:
            stage = rng.choice(vocab.UNREGISTERED_STAGES)
            log.record("deal.unregistered_stage_code")

        probability = rng.choice([0, 10, 25, 50, 70, 90, 100])
        record = {
            "dealno": f"D-{i:06d}",
            "custno": account["custno"],
            "persno": contact["persno"] if contact else None,
            "descr": rng.choice(vocab.DEAL_DESCRIPTIONS),
            "amount": _format_amount(rng, amount, dirty=dirty),
            "curr": rng.choice(["EUR", "EUR", "EUR", "eur", "€", "", "CHF"]),
            "stage_cd": stage,
            "prob": rng.choice([str(probability), f"{probability}%", f"{probability / 100:.2f}"]),
            "close_dt": _format_date(rng, close, dirty=dirty),
            "owner_id": rng.choice(users)["usr_id"],
            "del_flag": "Y" if rng.random() < 0.02 else "N",
            "created": _format_date(rng, created, dirty=dirty),
        }
        if dirty:
            log.record("deal.amount_or_date_format")
        if rng.random() < 0.015:
            record["amount"] = rng.choice(["", "n/c", "à définir", "-"])
            log.record("deal.unparseable_amount")
        if record["del_flag"] == "Y":
            log.record("deal.soft_deleted")
        deals.append(record)
    return deals


def _build_activities(
    rng: random.Random,
    settings: Settings,
    accounts: list[dict[str, Any]],
    contacts: list[dict[str, Any]],
    deals: list[dict[str, Any]],
    users: list[dict[str, Any]],
    log: DefectLog,
) -> list[dict[str, Any]]:
    activities: list[dict[str, Any]] = []
    live_accounts = [a for a in accounts if a["del_flag"] != "Y"]

    for i in range(1, settings.n_activities + 1):
        account = rng.choice(live_accounts)
        deal: dict[str, Any] | None = rng.choice(deals) if rng.random() < 0.4 else None
        contact: dict[str, Any] | None = rng.choice(contacts) if rng.random() < 0.7 else None
        dirty = rng.random() < settings.defect_rate
        when = _random_date(rng)

        subject = rng.choice(vocab.ACTIVITY_SUBJECTS)
        record = {
            "actno": f"A-{i:07d}",
            "custno": account["custno"],
            "persno": contact["persno"] if contact else None,
            "dealno": deal["dealno"] if deal else None,
            "act_type": rng.choice(vocab.ACTIVITY_TYPES),
            "subj": _mojibake(subject) if dirty and rng.random() < 0.3 else subject,
            "body": rng.choice(
                [
                    "",
                    "RAS",
                    "Le client demande un rappel la semaine prochaine.",
                    "Devis transmis, en attente de retour.",
                    "Attention: interlocuteur a changé.",
                ]
            ),
            "act_dt": _format_date(rng, when, dirty=dirty),
            "owner_id": rng.choice(users)["usr_id"],
            "del_flag": "Y" if rng.random() < 0.03 else "N",
        }
        if dirty and rng.random() < 0.3:
            log.record("activity.mojibake")
        if record["del_flag"] == "Y":
            log.record("activity.soft_deleted")
        activities.append(record)
    return activities


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

_INSERTS: dict[str, str] = {
    "usr": """INSERT INTO {s}.usr (usr_id, login, fullname, email, active)
              VALUES (:usr_id, :login, :fullname, :email, :active)""",
    "cust": """INSERT INTO {s}.cust (custno, company, addr1, addr2, city, zip, ctry, phone,
                                     fax, vat_no, ind_code, owner_id, status, del_flag, notes,
                                     created, updated)
               VALUES (:custno, :company, :addr1, :addr2, :city, :zip, :ctry, :phone,
                       :fax, :vat_no, :ind_code, :owner_id, :status, :del_flag, :notes,
                       :created, :updated)""",
    "person": """INSERT INTO {s}.person (persno, custno, fname, lname, title, email, phone,
                                         mobile, role_cd, opt_out, del_flag, created)
                 VALUES (:persno, :custno, :fname, :lname, :title, :email, :phone,
                         :mobile, :role_cd, :opt_out, :del_flag, :created)""",
    "deal": """INSERT INTO {s}.deal (dealno, custno, persno, descr, amount, curr, stage_cd,
                                     prob, close_dt, owner_id, del_flag, created)
               VALUES (:dealno, :custno, :persno, :descr, :amount, :curr, :stage_cd,
                       :prob, :close_dt, :owner_id, :del_flag, :created)""",
    "act": """INSERT INTO {s}.act (actno, custno, persno, dealno, act_type, subj, body,
                                   act_dt, owner_id, del_flag)
              VALUES (:actno, :custno, :persno, :dealno, :act_type, :subj, :body,
                      :act_dt, :owner_id, :del_flag)""",
}


def generate_legacy(settings: Settings | None = None) -> tuple[dict[str, int], DefectLog]:
    """Populate the legacy schema. Returns row counts and the defect log."""
    settings = settings or get_settings()
    rng = random.Random(settings.random_seed)
    log = DefectLog()

    logger.info("generating the legacy CRM", extra={"seed": settings.random_seed})

    users = _build_users(rng)
    accounts = _build_accounts(rng, settings, users, log)
    contacts = _build_contacts(rng, settings, accounts, log)
    deals = _build_deals(rng, settings, accounts, contacts, users, log)
    activities = _build_activities(rng, settings, accounts, contacts, deals, users, log)

    schema = settings.legacy_schema
    engine = get_engine(settings)
    with engine.begin() as conn:
        for table in ("act", "deal", "person", "cust", "usr", "ref_stage", "ref_industry"):
            conn.execute(text(f"TRUNCATE TABLE {schema}.{table} CASCADE"))

        conn.execute(
            text(
                f"""INSERT INTO {schema}.ref_stage (stage_cd, label, is_won, is_closed)
                    VALUES (:code, :label, :won, :closed)"""
            ),
            [
                {"code": c, "label": label, "won": won, "closed": closed}
                for c, label, won, closed in vocab.STAGES
            ],
        )
        conn.execute(
            text(f"INSERT INTO {schema}.ref_industry (ind_code, label) VALUES (:code, :label)"),
            [{"code": code, "label": label} for code, label in vocab.INDUSTRIES],
        )
        for table, rows in (
            ("usr", users),
            ("cust", accounts),
            ("person", contacts),
            ("deal", deals),
            ("act", activities),
        ):
            if rows:
                conn.execute(text(_INSERTS[table].format(s=schema)), rows)

    counts = {
        "usr": len(users),
        "cust": len(accounts),
        "person": len(contacts),
        "deal": len(deals),
        "act": len(activities),
    }
    logger.info("legacy CRM generated", extra={**counts, "defects": log.total})
    return counts, log


def write_activity_exports(settings: Settings | None = None) -> list[Path]:
    """Write the nightly activity export files.

    Semicolon-separated and encoded in cp1252, because that is what the old
    system's export job produced and what an Excel user on Windows expects. A
    migration tool that assumes UTF-8 and commas fails on the first accented
    subject line, which is roughly row four.
    """
    settings = settings or get_settings()
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    engine = get_engine(settings)
    schema = settings.legacy_schema

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""SELECT actno, custno, persno, dealno, act_type, subj, body,
                           act_dt, owner_id, del_flag
                    FROM {schema}.act ORDER BY actno"""
            )
        ).all()

    written: list[Path] = []
    per_file = max(1, len(rows) // 3 + 1)
    for index in range(0, len(rows), per_file):
        chunk = rows[index : index + per_file]
        path = settings.export_dir / f"ACT_EXPORT_{index // per_file + 1:02d}.csv"
        with path.open("w", encoding="cp1252", errors="replace", newline="") as handle:
            writer = csv.writer(handle, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(
                [
                    "ACTNO",
                    "CUSTNO",
                    "PERSNO",
                    "DEALNO",
                    "ACT_TYPE",
                    "SUBJ",
                    "BODY",
                    "ACT_DT",
                    "OWNER_ID",
                    "DEL_FLAG",
                ]
            )
            for row in chunk:
                writer.writerow(["" if value is None else value for value in row])
        written.append(path)

    logger.info("activity exports written", extra={"files": len(written), "rows": len(rows)})
    return written
