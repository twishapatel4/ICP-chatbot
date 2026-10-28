"""What the company can do on the review screen: edit fields, weights, required-versus-preferred
flags and exclusion rules, then approve (spec section 6.4). Each change returns the new document
and the feedback events to record; the first draft is never modified."""

from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from icp_chatbot.domain.slots import Track
from icp_chatbot.domain.state import SlotSource, normalize_values
from icp_chatbot.icp.document import (
    FieldEntry,
    ICPDocument,
    compute_completeness,
    group_by_section,
    render_summary,
    sync_field_requirements,
)
from icp_chatbot.icp.scoring import (
    Disqualifier,
    make_disqualifier,
    refresh_model,
    set_requirement,
    set_weights,
    validate_model,
)

EDITED_EVIDENCE = "edited by the company"


class FieldEdit(BaseModel):
    kind: Literal["field"] = "field"
    slot: str
    values: list[str]  # an empty list removes the field


class WeightEdit(BaseModel):
    kind: Literal["weights"] = "weights"
    weights: dict[str, float]  # criterion key -> weight; the new total must be 100


class RequirementEdit(BaseModel):
    kind: Literal["requirement"] = "requirement"
    criterion: str
    requirement: Literal["required", "preferred"]


class DisqualifierEdit(BaseModel):
    kind: Literal["disqualifiers"] = "disqualifiers"
    disqualifiers: list[Disqualifier]  # replaces the whole list


Edit = Annotated[
    FieldEdit | WeightEdit | RequirementEdit | DisqualifierEdit, Field(discriminator="kind")
]


@dataclass(frozen=True)
class FeedbackSpec:
    type: str  # field_edit, weight_edit, requirement_toggle, disqualifier_edit or approve
    field: str | None
    old: Any
    new: Any


def _rebuild(document: ICPDocument, entries: dict[str, FieldEntry], model) -> ICPDocument:
    rebuilt = document.model_copy(
        update={
            "sections": group_by_section(entries, document.slot_defs()),
            "scoring_model": model,
            "summary": render_summary(entries, document.slot_defs()),
            "completeness": compute_completeness(entries, document.needed_defs()),
        }
    )
    return sync_field_requirements(rebuilt)


def apply_edit(
    document: ICPDocument, edit: Edit, today: date
) -> tuple[ICPDocument, list[FeedbackSpec]]:
    entries = document.entries()
    model = document.scoring_model

    if isinstance(edit, FieldEdit):
        if edit.slot not in {s.key for s in document.slot_defs()}:
            raise ValueError(f"unknown field: {edit.slot}")
        old = entries[edit.slot].values if edit.slot in entries else []
        values = normalize_values(edit.values)
        if values:
            entries[edit.slot] = FieldEntry(
                values=values,
                confidence=1.0,
                source=SlotSource.STATED,
                evidence=EDITED_EVIDENCE,
                last_verified=today.isoformat(),
            )
        else:
            entries.pop(edit.slot, None)
        slot_values = {k: e.values for k, e in entries.items() if e.values}
        model = refresh_model(model, slot_values)
        return _rebuild(document, entries, model), [
            FeedbackSpec("field_edit", edit.slot, old, values)
        ]

    if isinstance(edit, WeightEdit):
        before = {c.key: c.weight for c in model.criteria}
        model = set_weights(model, edit.weights)
        after = {c.key: c.weight for c in model.criteria}
        specs = [
            FeedbackSpec("weight_edit", k, before[k], after[k])
            for k in after
            if after[k] != before[k]
        ]
        return _rebuild(document, entries, model), specs

    if isinstance(edit, RequirementEdit):
        before = next((c.requirement for c in model.criteria if c.key == edit.criterion), None)
        model = set_requirement(model, edit.criterion, edit.requirement)
        specs = [FeedbackSpec("requirement_toggle", edit.criterion, before, edit.requirement)]
        return _rebuild(document, entries, model), specs

    rules = []
    for d in edit.disqualifiers:
        rule = make_disqualifier(d.field, d.operator, d.values)
        if rule is None:
            raise ValueError(f"unusable exclusion rule: {d.field} {d.operator} {d.values}")
        rules.append(rule)
    old_rules = [d.model_dump() for d in model.disqualifiers]
    model = model.model_copy(update={"disqualifiers": rules})
    return _rebuild(document, entries, model), [
        FeedbackSpec("disqualifier_edit", None, old_rules, [r.model_dump() for r in rules])
    ]


def approve(
    document: ICPDocument, today: date, track: Track = "sales"
) -> tuple[ICPDocument, list[FeedbackSpec]]:
    """Approval verifies every field as of today. The scoring model must be valid."""
    problems = validate_model(document.scoring_model, track)
    if problems:
        raise ValueError("; ".join(problems))
    sections = {
        name: {
            slot: entry.model_copy(update={"last_verified": today.isoformat()})
            for slot, entry in fields.items()
        }
        for name, fields in document.sections.items()
    }
    return document.model_copy(update={"sections": sections}), [
        FeedbackSpec("approve", None, None, None)
    ]


def carry_over_edits(new_document: ICPDocument, previous: ICPDocument) -> ICPDocument:
    """After the bot rebuilds the ICP (for example after detail mode), keep what the company had
    already changed: edited fields, weights, required flags and exclusion rules. Fields the
    company removed are not carried over, because a removal leaves no trace in the document."""
    entries = new_document.entries()
    for slot, entry in previous.entries().items():
        if entry.evidence == EDITED_EVIDENCE:
            entries[slot] = entry
    slot_values = {k: e.values for k, e in entries.items() if e.values}
    model = refresh_model(previous.scoring_model, slot_values)
    return _rebuild(new_document, entries, model)
