"""a model provider that never leaves the box. it reads the brief the real model would get
(the json in the user prompt) and answers from the deterministic comparison, with a few
scripted opinions so the review flow has something to review."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from app.ai.provider import StructuredResult

_FENCE = re.compile(r"```json\n(.*?)\n```", re.S)


def extract_inputs(user_prompt: str) -> dict[str, Any]:
    match = _FENCE.search(user_prompt)
    assert match, "the user prompt must carry the brief as a json block"
    return json.loads(match.group(1))


def default_responder(inputs: dict[str, Any], call_no: int) -> dict[str, Any]:
    entity = inputs["dataset"]["entity_guess"] or "organization"
    mappings = []
    for f in inputs["fields"]:
        name = f["name"]
        cmp = f.get("deterministic_comparison") or {}
        cls = cmp.get("classification")
        ask = False
        if name == "acct_num":
            target, conf = f"{entity}.organization_id", 0.97
        elif name == "customer_tier":
            target, conf, ask = None, 0.4, True
        elif cls in ("MATCHED", "TRANSFORMATION_REQUIRED"):
            target, conf = cmp["target"], 0.92
        else:
            target, conf = None, 0.6
        transform = cls == "TRANSFORMATION_REQUIRED"
        mappings.append(
            {
                "source_field": name,
                "target_field": target,
                "confidence": conf,
                "reason": f"fake: comparison said {cls}",
                "transformation_required": transform,
                "transformation": "fake rule" if transform else None,
                "clarification_required": ask,
                "clarification_question": (
                    "Does customer_tier mean the plan or the account priority?" if ask else None
                ),
            }
        )
    return {
        "dataset": inputs["dataset"]["name"],
        "entity": entity,
        "mappings": mappings,
        "observations": [f"fake observation, call {call_no}"],
    }


def bad_target_responder(inputs: dict[str, Any], call_no: int) -> dict[str, Any]:
    proposal = default_responder(inputs, call_no)
    proposal["mappings"][0]["target_field"] = "organization.does_not_exist"
    return proposal


class FakeProvider:
    name = "fake"
    model = "fake-mapper-1"

    def __init__(
        self,
        responder: Callable[[dict[str, Any], int], dict[str, Any]] | None = None,
        *,
        fail_first: int = 0,
    ) -> None:
        self.responder = responder or default_responder
        self.fail_first = fail_first
        self.calls: list[dict[str, Any]] = []

    def complete(
        self, *, system: str, user: str, output: type, max_tokens: int
    ) -> StructuredResult:
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        inputs = extract_inputs(user)
        n = len(self.calls)
        payload = (
            bad_target_responder(inputs, n) if n <= self.fail_first else self.responder(inputs, n)
        )
        return StructuredResult(
            parsed=output.model_validate(payload),
            provider=self.name,
            model=self.model,
            latency_ms=1.5,
            input_tokens=1000,
            output_tokens=400,
            request_id=f"req_fake_{n}",
        )
