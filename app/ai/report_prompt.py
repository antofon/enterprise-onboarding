"""what the model is shown when it drafts a readiness report's executive summary.

The facts are computed before the model is asked: the status, every count, every percentage,
the gates and the work items. The model's job is the sentences: a summary a sponsor will read
and an explanation of each blocker in the customer's language. It cannot change the status and
it cannot introduce a number; the readiness service checks both and sends the problems back.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.ai.prompts import PromptBundle
from app.target.schema import PLATFORM_NAME

REPORT_PROMPT_VERSION = "2026-10-02.2"

SYSTEM_PROMPT = f"""You write the executive summary of an implementation readiness report inside an internal enterprise onboarding tool. An implementation engineer is migrating a customer's data into {PLATFORM_NAME}, a B2B SaaS platform. The report answers one question for the customer's sponsor: can this customer go live, and if not, what has to happen first.

You are given facts computed by deterministic code: the readiness status, the rehearsal's counts, the reconciliation result, the gates that decided the status, and the work items that stand between the customer and go-live. The facts are the whole truth available to you.

Rules:
- The status is decided. State it exactly as given, in capitals, in the headline. Never soften it, never suggest a different status, never use another status word.
- Every number you write must appear in the facts, written the same way (7,163 stays 7,163; 44.6% stays 44.6%). Do not add, subtract, round, or compute anything. If a number you want is not in the facts, describe it in words instead.
- Do not invent causes, owners, dates, or commitments. Say only what the facts support.
- Write for a sponsor who will read nothing else: plain English, concrete, short sentences, no jargon from this tool (no "namespace", "ledger", "gate"). Name the customer.
- The summary is one or two short paragraphs, under 180 words in total.
- For each work item listed under explain, write two or three sentences in the customer's language: what is wrong, why {PLATFORM_NAME} cannot take those records as they are, and what the customer has to do. Copy the code exactly.
- No em dashes. Use commas, colons or full stops.
- Answer in the requested structured format and nothing else."""


def build_summary_prompt(
    facts: dict[str, Any], *, explain: list[str], provider: str, model: str
) -> PromptBundle:
    inputs = {"facts": facts, "explain": explain}
    codes = ", ".join(explain) if explain else "none"
    user = (
        "Write the executive summary and the blocker explanations for this readiness report. "
        f"blocker_explanations must have exactly one entry for each of these codes, in this "
        f"order: {codes}.\n\n"
        "```json\n" + json.dumps(inputs, indent=1, default=str, ensure_ascii=False) + "\n```"
    )
    digest = hashlib.sha256(
        json.dumps(
            {
                "prompt_version": REPORT_PROMPT_VERSION,
                "system": SYSTEM_PROMPT,
                "provider": provider,
                "model": model,
                "inputs": inputs,
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    return PromptBundle(
        system=SYSTEM_PROMPT,
        user=user,
        inputs=inputs,
        input_hash=digest,
        provider=provider,
        model=model,
    )
