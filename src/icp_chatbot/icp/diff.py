"""Comparing the bot's draft with the company's final ICP: the main learning signal."""

from icp_chatbot.icp.document import ICPDocument


def _norm(values: list[str]) -> frozenset[str]:
    return frozenset(v.strip().lower() for v in values if v.strip())


def changed_fields(
    baseline: ICPDocument, final: ICPDocument
) -> dict[str, tuple[list[str], list[str]]]:
    """slot -> (draft values, final values) for every field the company changed, added or removed."""
    before, after = baseline.slot_values(), final.slot_values()
    return {
        slot: (before.get(slot, []), after.get(slot, []))
        for slot in sorted(set(before) | set(after))
        if _norm(before.get(slot, [])) != _norm(after.get(slot, []))
    }


def edit_rate(baseline: ICPDocument, final: ICPDocument) -> float:
    """Fields changed divided by the fields in the draft (at least 1). Lower is better."""
    changed = len(changed_fields(baseline, final))
    return changed / max(len(baseline.slot_values()), 1)
