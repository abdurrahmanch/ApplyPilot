"""The review gate: the single human touchpoint in the pipeline.

Every run prepares its applications end to end, then stops here. Abdur-Rahman
clears the batch in one sitting; approval is the ONLY thing that lets
submission start.

Design goal: a 17-application batch cleared in under 15 minutes. That makes
escalation the whole design. A gate that shows everything is a dump he will
stop reading, and a gate that hides a judgement call is worse. So an item
appears if and only if one of these holds (CLAUDE.md section 9):

  1. novel_question    — no bank match above the review floor
  2. low_confidence    — a match between the floor and the reuse threshold
  3. never_auto_guess  — work auth, sponsorship, or salary; every batch, forever
  4. cover_delta       — opening line and hooks only, not the whole letter
  5. sameness_warning  — from the batch-wide letter comparison
  6. disqualified      — one line, awareness only
  7. parked            — one line, awareness only

Everything else is silent by design. Batch state lives in `review_batches`;
`build_batch` assembles one, `approve_batch` opens the gate, and
`apply_gate_correction` feeds edits back into the question bank and the voice
register so the same correction is never needed twice.
"""

from applypilot.review.batch import (
    ITEM_KINDS,
    apply_gate_correction,
    approve_batch,
    batch_members,
    build_batch,
    cleared_urls,
    close_completed_batches,
    gate_state,
    get_batch,
    get_pending_batch,
    mark_batch_submitted,
    resolve_item,
    submittable_applications,
)

__all__ = [
    "ITEM_KINDS",
    "approve_batch",
    "apply_gate_correction",
    "batch_members",
    "build_batch",
    "cleared_urls",
    "close_completed_batches",
    "gate_state",
    "get_batch",
    "get_pending_batch",
    "mark_batch_submitted",
    "resolve_item",
    "submittable_applications",
]
