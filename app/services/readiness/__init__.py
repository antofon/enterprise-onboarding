"""the implementation readiness report: can this customer go live, and if not, what first.

One module per layer below, plus `service` (build, store and read reports back) and `render`
(the markdown export). This package re-exports what the api, the cli and the tests use.

Four layers, and only the last one may involve a model:

  facts        everything already recorded about the project, read back: the profile, the
               mapping decisions, the latest dry run, its reconciliation, every issue it found.
               no new numbers are made here, only counts of stored rows.
  gates        stated rules over the facts decide the status. any blocker is BLOCKED, any
               condition is READY WITH CONDITIONS, otherwise READY. each gate says what it
               checked, what it found, and which threshold it used, so a status can be argued
               with line by line.
  work         the issues grouped into what somebody has to do: decisions only the customer can
               make, data the customer has to correct, records that wait on another fix, and
               warnings to sign off. customer questions, next steps and technical risks follow
               from the work and the gates by rule.
  summary      a page a sponsor will read. drafted by the configured model from the facts and
               checked: the status must be stated as decided, every number must appear in the
               facts, every explanation must name a real work item. a draft that fails is sent
               back with the problems; after the last attempt, or with no model configured, the
               summary is written from the facts by code, and the report says which.
"""

from app.services.readiness.facts import collect_facts
from app.services.readiness.gates import assess
from app.services.readiness.render import render_markdown
from app.services.readiness.service import (
    build_content,
    generate_report,
    get_report,
    latest_report,
    list_reports,
    preview,
)
from app.services.readiness.summary import (
    _explain_codes,
    check_summary,
    draft_summary,
    summary_facts,
    template_summary,
)
from app.services.readiness.vocabulary import ACTIONS, BLOCKER, CONDITION, PASS
from app.services.readiness.work import (
    customer_questions,
    next_steps,
    technical_risks,
    work_items,
)

__all__ = [
    "ACTIONS",
    "BLOCKER",
    "CONDITION",
    "PASS",
    "_explain_codes",
    "assess",
    "build_content",
    "check_summary",
    "collect_facts",
    "customer_questions",
    "draft_summary",
    "generate_report",
    "get_report",
    "latest_report",
    "list_reports",
    "next_steps",
    "preview",
    "render_markdown",
    "summary_facts",
    "technical_risks",
    "template_summary",
    "work_items",
]
