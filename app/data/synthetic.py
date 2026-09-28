"""reproducible synthetic customer: Apex Equipment Services.

three source systems, each with its own bad habits:
  legacy crm export   organizations.csv, contacts.csv   free-text statuses, mixed date formats,
                                                        duplicate accounts, "CA" meaning two things
  billing system      subscriptions.json                plan names that match nothing, amounts as
                                                        strings with currency symbols, quarterly
                                                        billing nobody supports anymore
  activity export     activity.csv                      numeric fields stored as text, mixed
                                                        timestamps

every defect is injected at a known rate and counted in manifest.json, so tests and the docs cite
real numbers. the same seed always produces byte-identical files.
"""

from __future__ import annotations

import csv
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from faker import Faker

CUSTOMER_NAME = "Apex Equipment Services"
GENERATOR_VERSION = 1
EXPORT_DATE = date(2026, 9, 15)  # the day the customer sent us the files, in the fiction

# --- pools ---------------------------------------------------------------------------------

STATUS_POOL = {
    "Active": 52,
    "ACTIVE": 6,
    "active": 6,
    "Inactive": 10,
    "Prospect": 6,
    "Churned": 5,
    "On Hold": 4,
    "Closed": 3,
    "Actve": 1,
    "": 4,
}
UNKNOWN_STATUS = {"On Hold", "Closed", "Actve"}

INDUSTRY_POOL = {
    "MFG": 30,
    "CONST": 15,
    "ENR": 8,
    "LOG": 12,
    "HC": 6,
    "TECH": 8,
    "PS": 6,
    "OTH": 5,
    "31": 3,
    "23": 2,
    "": 5,
}
UNKNOWN_INDUSTRY = {"31", "23"}

US_STATES = [
    ("CA", "California", "Calif."),
    ("TX", "Texas", "Tex."),
    ("OH", "Ohio", "Ohio"),
    ("PA", "Pennsylvania", "Penn."),
    ("IL", "Illinois", "Ill."),
    ("MI", "Michigan", "Mich."),
    ("GA", "Georgia", "Ga."),
    ("NC", "North Carolina", "N.C."),
    ("WA", "Washington", "Wash."),
    ("AZ", "Arizona", "Ariz."),
    ("CO", "Colorado", "Colo."),
    ("IN", "Indiana", "Ind."),
    ("TN", "Tennessee", "Tenn."),
    ("FL", "Florida", "Fla."),
    ("NY", "New York", "N.Y."),
]
CA_PROVINCES = [
    ("ON", "Ontario", "Ont."),
    ("AB", "Alberta", "Alta."),
    ("BC", "British Columbia", "B.C."),
    ("QC", "Quebec", "Que."),
]

COUNTRY_VARIANTS = {
    "US": {"US": 45, "USA": 25, "United States": 15, "U.S.": 8, "": 7},
    "CA": {"Canada": 55, "CA": 30, "CAN": 15},
    "MX": {"Mexico": 60, "MX": 40},
}

TIER_POOL = {"Gold": 25, "Silver": 30, "Bronze": 20, "Strategic": 8, "Platinum": 3, "": 14}

ROLE_POOL = {
    "Billing": 14,
    "billing contact": 6,
    "AP": 4,
    "CTO": 6,
    "Primary": 12,
    "Main": 4,
    "Technical": 10,
    "IT": 6,
    "CEO": 4,
    "Owner": 5,
    "Ops Manager": 8,
    "Purchasing": 6,
    "": 15,
}
PRIMARY_FLAG = {"Y": 30, "N": 30, "yes": 8, "no": 8, "TRUE": 6, "FALSE": 6, "1": 4, "0": 4, "": 4}

PLAN_POOL = {
    "Pro": 18,
    "Professional": 15,
    "Professional Plus": 6,
    "Enterprise": 20,
    "Enterprise v2": 8,
    "ENT": 4,
    "Starter": 15,
    "Basic": 6,
    "Legacy Gold": 5,
    "Trial": 3,
}
ENTERPRISE_LIKE = {"Enterprise", "Enterprise v2", "ENT", "Legacy Gold"}
BILLING_FREQ_POOL = {"M": 30, "A": 25, "monthly": 20, "annual": 12, "yearly": 10, "Q": 3}
SUB_STATUS_POOL = {
    "active": 45,
    "Active": 15,
    "paused": 5,
    "past due": 6,
    "past_due": 4,
    "canceled": 6,
    "cancelled": 6,
    "trialing": 8,
    "expired": 5,
}
ACTIVITY_TYPES = {
    "Invoice": 25,
    "INVOICE": 5,
    "Payment": 22,
    "Ticket": 12,
    "Support Ticket": 8,
    "Login": 15,
    "Note": 8,
    "Call": 5,
}

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%Y/%m/%d", "%B %d, %Y", "%m/%d/%y"]


@dataclass
class Summary:
    seed: int
    requested_rows: int
    out_dir: Path
    files: dict[str, int] = field(default_factory=dict)
    defects: Counter[str] = field(default_factory=Counter)

    def as_manifest(self) -> dict[str, Any]:
        return {
            "customer": CUSTOMER_NAME,
            "generator_version": GENERATOR_VERSION,
            "seed": self.seed,
            "requested_rows": self.requested_rows,
            "files": dict(sorted(self.files.items())),
            "defects": dict(sorted(self.defects.items())),
        }


class _Gen:
    def __init__(self, rows: int, seed: int) -> None:
        self.rows = rows
        self.rng = random.Random(seed)
        self.fake = Faker("en_US")
        self.fake.seed_instance(seed)
        self.defects: Counter[str] = Counter()
        self.next_contact = 1
        self.next_sub = 1
        self.next_activity = 1

    # --- helpers ---------------------------------------------------------------------------

    def pick(self, pool: dict[str, int]) -> str:
        return self.rng.choices(list(pool), weights=list(pool.values()), k=1)[0]

    def chance(self, rate: float) -> bool:
        return self.rng.random() < rate

    def defect(self, name: str) -> None:
        self.defects[name] += 1

    def messy_date(self, d: date, blank_rate: float = 0.01) -> str:
        if self.chance(blank_rate):
            self.defect("date_blank")
            return ""
        if self.chance(0.004):
            self.defect("date_in_future")
            d = EXPORT_DATE + timedelta(days=self.rng.randint(30, 400))
        fmt = self.rng.choice(DATE_FORMATS)
        if fmt != "%Y-%m-%d":
            self.defect("date_non_iso")
        return d.strftime(fmt)

    def messy_datetime(self, dt: datetime) -> str:
        style = self.rng.random()
        if style < 0.5:
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        if style < 0.75:
            self.defect("datetime_non_iso")
            return dt.strftime("%m/%d/%Y %H:%M")
        if style < 0.9:
            self.defect("datetime_non_iso")
            return dt.strftime("%d-%b-%Y %I:%M %p")
        return dt.strftime("%Y-%m-%d %H:%M:%S-05:00")

    def whitespace(self, value: str, rate: float = 0.04) -> str:
        if value and self.chance(rate):
            self.defect("stray_whitespace")
            return f"  {value} " if self.rng.random() < 0.5 else f"{value}  "
        return value

    def messy_phone(self) -> str:
        area, exch, line = (
            self.rng.randint(201, 989),
            self.rng.randint(200, 999),
            self.rng.randint(0, 9999),
        )
        n = f"{area}{exch}{line:04d}"
        style = self.rng.random()
        if style < 0.30:
            return f"({n[:3]}) {n[3:6]}-{n[6:]}"
        if style < 0.55:
            return f"{n[:3]}-{n[3:6]}-{n[6:]}"
        if style < 0.70:
            return f"{n[:3]}.{n[3:6]}.{n[6:]}"
        if style < 0.82:
            return f"+1 {n[:3]} {n[3:6]} {n[6:]}"
        if style < 0.92:
            return n
        if style < 0.96:
            self.defect("phone_too_short")
            return f"{n[3:6]}-{n[6:]}"
        self.defect("phone_blank")
        return ""

    def messy_email(self, first: str, last: str, domain: str) -> str:
        local = f"{first}.{last}".lower().replace("'", "").replace(" ", "")
        email = f"{local}@{domain}"
        if self.chance(0.04):
            self.defect("email_malformed")
            return self.rng.choice(
                [f"{local}@", f"{local} at {domain}", f"{local}@{domain}.", local]
            )
        if self.chance(0.10):
            self.defect("email_uppercase")
            return email.upper()
        return email

    # --- organizations -----------------------------------------------------------------------

    def organizations(self) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        """returns (rows as written, canonical truth used to keep children consistent)."""
        rows: list[dict[str, str]] = []
        truth: list[dict[str, Any]] = []
        for i in range(self.rows):
            acct = str(10000 + i)
            name = self.fake.company()
            domain = name.lower().split(",")[0].split(" ")[0].strip(".-") + ".com"
            country = self.rng.choices(["US", "CA", "MX"], weights=[82, 15, 3], k=1)[0]
            region = (
                self.rng.choice(US_STATES if country == "US" else CA_PROVINCES)
                if country != "MX"
                else None
            )
            created = EXPORT_DATE - timedelta(days=self.rng.randint(90, 3600))
            last_activity = created + timedelta(
                days=self.rng.randint(0, (EXPORT_DATE - created).days)
            )
            status = self.pick(STATUS_POOL)
            tier = self.pick(TIER_POOL)
            if status in UNKNOWN_STATUS:
                self.defect("status_unknown_enum")
            elif status == "":
                self.defect("status_blank")
            elif status != status.title():
                self.defect("status_case_variant")

            # business rule conflict: an "Active" account with no activity in 18+ months
            if status.lower() == "active" and self.chance(0.06):
                last_activity = EXPORT_DATE - timedelta(days=self.rng.randint(560, 1200))
                self.defect("rule_active_but_dormant")

            truth.append(
                {
                    "acct": acct,
                    "name": name,
                    "domain": domain,
                    "country": country,
                    "status": status,
                    "tier": tier,
                    "created": created,
                    "last_activity": last_activity,
                }
            )

            industry = self.pick(INDUSTRY_POOL)
            if industry in UNKNOWN_INDUSTRY:
                self.defect("industry_unknown_code")

            if region is None:
                state = ""
            else:
                style = self.rng.random()
                if style < 0.55:
                    state = region[0]
                elif style < 0.90:
                    state = region[1]
                    self.defect("state_full_name")
                elif style < 0.95:
                    state = region[2]
                    self.defect("state_abbrev_variant")
                else:
                    state = ""
            country_text = self.pick(COUNTRY_VARIANTS[country])
            if country_text == "":
                self.defect("country_blank")
            elif country_text not in ("US", "CA", "MX"):
                self.defect("country_non_iso")
            if country == "CA" and country_text == "CA":
                self.defect("country_CA_ambiguous")

            web_style = self.rng.random()
            if web_style < 0.55:
                website = f"https://www.{domain}"
            elif web_style < 0.75:
                website = f"www.{domain}"
                self.defect("website_no_scheme")
            elif web_style < 0.85:
                website = "N/A"
                self.defect("website_placeholder")
            else:
                website = ""

            rev = self.rng.randint(2, 900) * 50_000
            rev_style = self.rng.random()
            if rev_style < 0.40:
                revenue = f"${rev:,}"
                self.defect("number_with_symbols")
            elif rev_style < 0.70:
                revenue = str(rev)
            elif rev_style < 0.80:
                revenue = f"{rev / 1_000_000:.2f}M"
                self.defect("number_with_symbols")
            elif rev_style < 0.92:
                revenue = ""
            else:
                revenue = "n/a"
                self.defect("number_placeholder")

            emp = self.rng.randint(5, 4000)
            emp_style = self.rng.random()
            if emp_style < 0.60:
                employees = str(emp)
            elif emp_style < 0.75:
                employees = f"{emp}.0"
                self.defect("number_as_float_text")
            elif emp_style < 0.88:
                employees = ""
            else:
                employees = "unknown"
                self.defect("number_placeholder")

            first, last = self.fake.first_name(), self.fake.last_name()
            rows.append(
                {
                    "acct_num": "" if self.chance(0.01) else acct,
                    "company_name": self.whitespace(name),
                    "company_status": status,
                    "industry_code": industry,
                    "hq_state": state,
                    "country": country_text,
                    "website": website,
                    "created_on": self.messy_date(created),
                    "customer_tier": tier,
                    "annual_revenue": revenue,
                    "employee_count": self.whitespace(employees, 0.06),
                    "primary_contact_email": self.messy_email(first, last, domain),
                    "account_owner": self.fake.name(),
                    "last_activity_date": self.messy_date(last_activity, blank_rate=0.03),
                    "notes": self.rng.choice(
                        [
                            "",
                            "",
                            "",
                            "renewal call scheduled",
                            "moved HQ 2024",
                            "do not contact billing before 9am",
                            "acquired by parent co",
                        ]
                    ),
                }
            )
            if rows[-1]["acct_num"] == "":
                self.defect("org_missing_id")

        # duplicate organizations: same account twice, or same company under a new account
        for _ in range(max(1, int(self.rows * 0.03))):
            src = self.rng.choice(rows)
            dup = dict(src)
            variant = self.rng.random()
            if variant < 0.35:
                dup["company_name"] = src["company_name"].upper()
            elif variant < 0.70:
                dup["company_name"] = src["company_name"].strip() + " Inc"
            else:
                dup["company_name"] = src["company_name"].strip() + "  "
            if self.rng.random() < 0.5:
                self.defect("org_duplicate_same_id")
            else:
                dup["acct_num"] = str(90000 + self.rng.randint(1, 9999))
                self.defect("org_duplicate_new_id")
            rows.append(dup)
        self.rng.shuffle(rows)
        return rows, truth

    # --- contacts --------------------------------------------------------------------------

    def contacts(self, truth: list[dict[str, Any]]) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for org in truth:
            n = self.rng.choices([1, 2, 3, 4], weights=[25, 40, 25, 10], k=1)[0]
            primaries = 1 if self.chance(0.85) else (2 if self.chance(0.20) else 0)
            if primaries == 2:
                self.defect("contact_two_primaries")
            elif primaries == 0:
                self.defect("contact_no_primary")
            for j in range(n):
                first, last = self.fake.first_name(), self.fake.last_name()
                name_style = self.rng.random()
                if name_style < 0.70:
                    full = f"{first} {last}"
                elif name_style < 0.82:
                    full = f"{last.upper()}, {first.upper()}"
                    self.defect("name_last_first")
                elif name_style < 0.92:
                    title = self.rng.choice(["Dr.", "Mr.", "Ms."])
                    initial = self.rng.choice("ABCDEFGHJKLMNPRS")
                    full = f"{title} {first} {initial}. {last}"
                    self.defect("name_with_title")
                else:
                    full = f" {first}  {last} "
                    self.defect("stray_whitespace")

                if j < primaries:
                    flag = self.pick({"Y": 40, "yes": 15, "TRUE": 15, "1": 10, "": 0} | {"Y": 40})
                else:
                    flag = self.pick({"N": 40, "no": 15, "FALSE": 15, "0": 10, "": 10})
                if flag not in ("Y", "N"):
                    self.defect("boolean_variant")

                acct = org["acct"]
                if self.chance(0.02):
                    acct = str(70000 + self.rng.randint(1, 9999))
                    self.defect("contact_dangling_account")
                elif self.chance(0.01):
                    acct = ""
                    self.defect("contact_missing_account")

                touch = org["last_activity"] - timedelta(days=self.rng.randint(0, 200))
                rows.append(
                    {
                        "contact_ref": f"CT-{self.next_contact:06d}",
                        "acct_num": acct,
                        "full_name": full,
                        "contact_mail": self.messy_email(first, last, org["domain"]),
                        "phone_number": self.messy_phone(),
                        "contact_role": self.pick(ROLE_POOL),
                        "is_primary": flag,
                        "last_touch": self.messy_date(touch, blank_rate=0.05),
                        "opt_out": self.pick({"Y": 10, "N": 85, "": 5}),
                    }
                )
                self.next_contact += 1

        for _ in range(max(1, int(len(rows) * 0.03))):
            src = self.rng.choice(rows)
            dup = dict(src)
            dup["contact_ref"] = f"CT-{self.next_contact:06d}"
            self.next_contact += 1
            if self.rng.random() < 0.5:
                dup["contact_mail"] = src["contact_mail"].upper()
            else:
                dup["full_name"] = src["full_name"].strip().title()
            self.defect("contact_duplicate")
            rows.append(dup)
        self.rng.shuffle(rows)
        return rows

    # --- subscriptions ---------------------------------------------------------------------

    def subscriptions(self, truth: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for org in truth:
            n = self.rng.choices([0, 1, 2], weights=[10, 80, 10], k=1)[0]
            for _ in range(n):
                plan = self.pick(PLAN_POOL)
                if org["tier"] == "Strategic" and plan not in ENTERPRISE_LIKE:
                    self.defect("rule_strategic_not_enterprise")
                if plan in (
                    "Legacy Gold",
                    "Enterprise v2",
                    "ENT",
                    "Professional Plus",
                    "Basic",
                    "Pro",
                ):
                    self.defect("plan_name_nonstandard")
                freq = self.pick(BILLING_FREQ_POOL)
                if freq == "Q":
                    self.defect("billing_freq_unknown_enum")
                status = self.pick(SUB_STATUS_POOL)
                if status in ("paused", "expired"):
                    self.defect("sub_status_unknown_enum")
                if org["status"].lower() in ("inactive", "churned") and status.lower() in (
                    "active",
                    "trialing",
                ):
                    self.defect("rule_inactive_org_active_sub")

                start = org["created"] + timedelta(days=self.rng.randint(0, 400))
                if start > EXPORT_DATE:
                    start = EXPORT_DATE - timedelta(days=self.rng.randint(1, 60))
                renew = start + timedelta(days=365 if freq in ("A", "annual", "yearly") else 30)
                if self.chance(0.02):
                    renew = start - timedelta(days=self.rng.randint(1, 90))
                    self.defect("renewal_before_start")
                if status == "trialing" and (EXPORT_DATE - start).days > 90:
                    self.defect("rule_trial_older_than_90d")

                seats = self.rng.choice([5, 10, 15, 25, 40, 60, 100, 250])
                seat_style = self.rng.random()
                seat_val: Any
                if seat_style < 0.70:
                    seat_val = seats
                elif seat_style < 0.95:
                    seat_val = str(seats)
                    self.defect("number_as_text")
                else:
                    seat_val = None
                    self.defect("seats_missing")

                amount = seats * {"Starter": 12, "Basic": 12, "Trial": 0}.get(
                    plan, 85 if plan in ("Pro", "Professional", "Professional Plus") else 160
                )
                amt_style = self.rng.random()
                amount_val: Any
                if amt_style < 0.40:
                    amount_val = f"${amount:,.2f}"
                    self.defect("number_with_symbols")
                elif amt_style < 0.70:
                    amount_val = amount
                elif amt_style < 0.85:
                    amount_val = str(amount)
                    self.defect("number_as_text")
                else:
                    amount_val = f"USD {amount}"
                    self.defect("number_with_symbols")

                acct = org["acct"]
                if self.chance(0.015):
                    acct = str(80000 + self.rng.randint(1, 9999))
                    self.defect("sub_dangling_account")

                rows.append(
                    {
                        "sub_id": f"SUB-{self.next_sub:05d}",
                        "acct_num": acct,
                        "subscription_level": plan,
                        "billing_freq": freq,
                        "sub_status": status,
                        "seat_count": seat_val,
                        "start_dt": self.messy_date(start),
                        "renew_dt": self.messy_date(renew),
                        "monthly_amount": amount_val,
                        "currency": "CAD"
                        if (org["country"] == "CA" and self.chance(0.4))
                        else "USD",
                    }
                )
                self.next_sub += 1
        self.rng.shuffle(rows)
        return rows

    # --- activity --------------------------------------------------------------------------

    def activity(self, truth: list[dict[str, Any]]) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for org in truth:
            for _ in range(self.rng.randint(2, 8)):
                kind = self.pick(ACTIVITY_TYPES)
                when = datetime.combine(
                    org["created"]
                    + timedelta(
                        days=self.rng.randint(
                            0, max(1, (org["last_activity"] - org["created"]).days)
                        )
                    ),
                    datetime.min.time(),
                ) + timedelta(hours=self.rng.randint(7, 19), minutes=self.rng.randint(0, 59))
                amount = ""
                if kind.lower() in ("invoice", "payment"):
                    val = self.rng.randint(1, 400) * 25
                    amount = self.rng.choice([f"{val:,}.00", str(val), f"{val}.0", "-"])
                    if amount == "-":
                        self.defect("number_placeholder")
                    elif "," in amount:
                        self.defect("number_with_symbols")
                rows.append(
                    {
                        "activity_id": f"ACT-{self.next_activity:07d}",
                        "acct_num": org["acct"],
                        "activity_type": kind,
                        "occurred_at": self.messy_datetime(when),
                        "amount": amount,
                        "currency": "USD" if amount else "",
                        "description": self.rng.choice(
                            [
                                "",
                                "",
                                "monthly invoice",
                                "payment received",
                                "portal login",
                                "asked about seat pricing",
                                "escalated: parts delay",
                                "renewal discussion",
                            ]
                        ),
                    }
                )
                self.next_activity += 1
        self.rng.shuffle(rows)
        return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def generate(
    rows: int = 10_000, seed: int = 42, out_dir: str | Path = "sample_customer/data"
) -> Summary:
    """write the customer's source files into out_dir and return what was produced."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    gen = _Gen(rows=rows, seed=seed)

    orgs, truth = gen.organizations()
    contacts = gen.contacts(truth)
    subs = gen.subscriptions(truth)
    acts = gen.activity(truth)

    _write_csv(out / "organizations.csv", orgs)
    _write_csv(out / "contacts.csv", contacts)
    _write_csv(out / "activity.csv", acts)
    (out / "subscriptions.json").write_text(
        json.dumps(
            {
                "system": "LegacyBill 4.2",
                "exported_at": f"{EXPORT_DATE.isoformat()}T06:00:00Z",
                "record_count": len(subs),
                "data": subs,
            },
            indent=2,
        )
        + "\n"
    )

    summary = Summary(seed=seed, requested_rows=rows, out_dir=out, defects=gen.defects)
    summary.files = {
        "organizations.csv": len(orgs),
        "contacts.csv": len(contacts),
        "subscriptions.json": len(subs),
        "activity.csv": len(acts),
    }
    (out / "manifest.json").write_text(json.dumps(summary.as_manifest(), indent=2) + "\n")
    return summary
