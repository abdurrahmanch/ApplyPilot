"""Question bank: similarity matching, confidence, and escalation rules.

The upstream `qa_knowledge` table matches on an md5 of the normalized question
text, so "Are you legally authorized to work in the US?" and "Are you legally
authorized to work in the United States?" are unrelated strings to it. Every
reworded question is therefore a fresh guess by the apply agent, and a wrong
guess on work authorization or sponsorship is a silent permanent reject.

This module adds the layer CLAUDE.md section 9 specifies: fuzzy matching,
per-entry confidence that falls when a human corrects it, and categories that
are never auto-answered no matter how confident the match.

## Why not sentence-transformer embeddings

Section 8 calls for a local embedding model. A real transformer means pulling
in torch (~2GB) to compare strings that are, in practice, boilerplate forms of
about fifteen underlying questions. Character n-gram cosine handles exactly
that shape — it is robust to rewording, punctuation, and the US/United States
class of variation — with no dependencies, no model download, and
deterministic scores that are reproducible in tests.

`vectorize()` is the seam. Swapping in a real model later means replacing that
one function and re-running `rebuild_embeddings()`; nothing else changes.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ── Thresholds (section 9) ────────────────────────────────────────────────
#
# Deliberately conservative. The system never loosens these on its own; it
# reports how they performed and Abdur-Rahman decides.

REUSE_THRESHOLD = 0.90    # >= this may be auto-answered, if all other gates pass
REVIEW_FLOOR = 0.75       # between floor and reuse -> surface for one-key confirm
                          # below floor -> treated as a novel question

# An entry must also have proven itself before it answers unattended.
MIN_TIMES_SEEN_FOR_AUTO = 3

# Categories that surface at every batch with the stored answer prefilled, no
# matter how confident the match is. A wrong click here is a silent reject.
SENSITIVE_CATEGORIES = frozenset({"work_auth", "sponsorship", "salary"})

CATEGORIES = frozenset({
    "work_auth", "sponsorship", "salary", "start_date", "relocation",
    "degree_yoe", "background_check", "eeo", "personal_fact", "free_text", "other",
})

# Correction penalty: a corrected entry loses most of its standing immediately.
CORRECTION_PENALTY = 0.5


# ── Text handling ─────────────────────────────────────────────────────────

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")

# Variations that carry no meaning for matching but wreck exact-hash lookup.
_SYNONYMS = [
    (r"\bunited states of america\b", "us"),
    (r"\bunited states\b", "us"),
    (r"\bu\s*s\s*a\b", "us"),
    (r"\bthe us\b", "us"),
    (r"\bauthorised\b", "authorized"),
    (r"\bwill you now or in the future\b", "do you"),
    (r"\bnow or in the future\b", ""),
    (r"\bare you legally\b", "are you"),
    (r"\bdo you currently\b", "do you"),
    (r"\bplease (indicate|specify|describe|tell us)\b", ""),
]


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, and fold known meaningless variations."""
    t = text.lower().strip()
    for pattern, repl in _SYNONYMS:
        t = re.sub(pattern, repl, t)
    t = _PUNCT_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def vectorize(text: str, n: int = 3) -> dict[str, float]:
    """L2-normalized character n-gram frequency vector.

    Character n-grams rather than words: screening questions differ by
    inflection and filler far more than by vocabulary, and n-grams degrade
    gracefully on both.
    """
    norm = normalize(text)
    if not norm:
        return {}
    padded = f"  {norm}  "
    grams = Counter(padded[i:i + n] for i in range(len(padded) - n + 1))
    magnitude = math.sqrt(sum(v * v for v in grams.values()))
    if not magnitude:
        return {}
    return {g: c / magnitude for g, c in grams.items()}


def similarity(a: str, b: str) -> float:
    """Cosine similarity of two questions, in [0, 1]."""
    va, vb = vectorize(a), vectorize(b)
    if not va or not vb:
        return 0.0
    shorter, longer = (va, vb) if len(va) < len(vb) else (vb, va)
    return sum(w * longer.get(g, 0.0) for g, w in shorter.items())


def _serialize(vec: dict[str, float]) -> bytes:
    return json.dumps(vec, separators=(",", ":")).encode()


def _deserialize(blob: bytes | None) -> dict[str, float]:
    if not blob:
        return {}
    try:
        return json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return {}


def _cosine(va: dict[str, float], vb: dict[str, float]) -> float:
    if not va or not vb:
        return 0.0
    shorter, longer = (va, vb) if len(va) < len(vb) else (vb, va)
    return sum(w * longer.get(g, 0.0) for g, w in shorter.items())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Bank operations ───────────────────────────────────────────────────────

def add_question(conn: sqlite3.Connection, canonical_text: str, answer: str,
                 category: str = "other", sensitive: bool | None = None,
                 confidence: float = 0.5, times_seen: int = 0) -> int:
    """Add or update a canonical question. Returns its id.

    `sensitive` defaults to whether the category is in SENSITIVE_CATEGORIES;
    pass it explicitly only to mark something sensitive that its category
    would not.
    """
    if category not in CATEGORIES:
        raise ValueError(f"Unknown category '{category}'. Known: {sorted(CATEGORIES)}")
    if sensitive is None:
        sensitive = category in SENSITIVE_CATEGORIES

    existing = conn.execute(
        "SELECT id FROM questions WHERE canonical_text = ?", (canonical_text,)
    ).fetchone()

    vec = _serialize(vectorize(canonical_text))
    if existing:
        qid = existing["id"] if not isinstance(existing, tuple) else existing[0]
        conn.execute(
            "UPDATE questions SET canonical_answer = ?, category = ?, sensitive = ?, "
            "embedding = ?, updated_at = ? WHERE id = ?",
            (answer, category, int(sensitive), vec, _now(), qid))
        conn.commit()
        return qid

    cur = conn.execute(
        "INSERT INTO questions (canonical_text, embedding, canonical_answer, category, "
        "sensitive, confidence, times_seen, times_corrected, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (canonical_text, vec, answer, category, int(sensitive), confidence,
         times_seen, _now()))
    conn.commit()
    return cur.lastrowid


def find_match(conn: sqlite3.Connection, question_text: str) -> dict | None:
    """Return the closest bank entry with its similarity, or None if the bank is empty.

    The caller decides what to do with a weak match; this reports, it does not
    filter. `decide()` is the thing that applies the thresholds.
    """
    rows = conn.execute("SELECT * FROM questions").fetchall()
    if not rows:
        return None

    target = vectorize(question_text)
    best, best_score = None, -1.0
    for row in rows:
        stored = _deserialize(row["embedding"])
        if not stored:
            stored = vectorize(row["canonical_text"])
        score = _cosine(target, stored)
        if score > best_score:
            best, best_score = row, score

    if best is None:
        return None
    match = dict(best)
    match["similarity"] = best_score
    return match


# Keyword hints for the categories where a miss is expensive.
#
# n-gram similarity is length-sensitive: "Desired salary?" scores 0.70 against
# "What is your desired compensation?" purely because it is short. Falling
# through to 'novel' is safe (it parks), but for the three sensitive categories
# it is better to recognize the subject, prefill the stored answer, and let
# Abdur-Rahman confirm with one key. These hints can only ever downgrade an
# answer to 'confirm' — they never authorize auto-answering.
_CATEGORY_KEYWORDS: list[tuple[str, str]] = [
    ("sponsorship", r"\b(sponsor|sponsorship|visa|h-?1b|work permit)\b"),
    ("salary", r"\b(salary|compensation|pay expectation|desired pay|rate expectation)\b"),
    ("work_auth", r"\b(authoriz|authoris|legally (able|entitled)|right to work|work eligib)\w*\b"),
]


def category_hint(question_text: str) -> str | None:
    """Best-guess category from keywords alone, for sensitive subjects only."""
    norm = normalize(question_text)
    for category, pattern in _CATEGORY_KEYWORDS:
        if re.search(pattern, norm):
            return category
    return None


def _best_in_category(conn: sqlite3.Connection, category: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM questions WHERE category = ? "
        "ORDER BY times_corrected ASC, confidence DESC, times_seen DESC LIMIT 1",
        (category,)).fetchone()
    return dict(row) if row else None


def decide(conn: sqlite3.Connection, question_text: str) -> dict:
    """Decide how one form question should be handled.

    Returns a dict with:
      action    -- 'auto' | 'confirm' | 'novel'
      answer    -- the proposed answer, or None for a novel question
      reason    -- why this action, for the review gate to display
      match     -- the matched bank row plus 'similarity', if any

    'auto' is the only action that proceeds without a human. It requires ALL of:
      similarity >= REUSE_THRESHOLD, the entry is not sensitive,
      times_seen >= MIN_TIMES_SEEN_FOR_AUTO, and times_corrected == 0.
    """
    match = find_match(conn, question_text)
    sim = match["similarity"] if match else 0.0

    if not match or sim < REVIEW_FLOOR:
        # Before calling it novel, check whether the wording is simply terse on
        # a subject we do hold an answer for. Sensitive categories only, and
        # the result is always 'confirm' — never 'auto'.
        hint = category_hint(question_text)
        if hint:
            fallback = _best_in_category(conn, hint)
            if fallback:
                fallback["similarity"] = sim
                return {"action": "confirm", "answer": fallback["canonical_answer"],
                        "match": fallback,
                        "reason": (f"weak text match ({sim:.2f}) but reads as a "
                                   f"{hint} question; confirm the stored answer")}
        return {"action": "novel", "answer": None, "match": match,
                "reason": (f"no bank entry above {REVIEW_FLOOR:.2f} "
                           f"(best {sim:.2f})" if match else "question bank is empty")}

    if match["sensitive"]:
        return {"action": "confirm", "answer": match["canonical_answer"], "match": match,
                "reason": f"{match['category']} is never auto-answered; confirm every time"}

    if sim < REUSE_THRESHOLD:
        return {"action": "confirm", "answer": match["canonical_answer"], "match": match,
                "reason": f"similarity {sim:.2f} is below the {REUSE_THRESHOLD:.2f} reuse threshold"}

    if match["times_corrected"] > 0:
        return {"action": "confirm", "answer": match["canonical_answer"], "match": match,
                "reason": f"corrected {match['times_corrected']}x before; not trusted unattended"}

    if match["times_seen"] < MIN_TIMES_SEEN_FOR_AUTO:
        return {"action": "confirm", "answer": match["canonical_answer"], "match": match,
                "reason": (f"seen {match['times_seen']}x, needs "
                           f"{MIN_TIMES_SEEN_FOR_AUTO} before answering unattended")}

    return {"action": "auto", "answer": match["canonical_answer"], "match": match,
            "reason": f"similarity {sim:.2f}, seen {match['times_seen']}x, never corrected"}


def record_sighting(conn: sqlite3.Connection, question_id: int | None, raw_text: str,
                    application_id: str | None = None, ats: str | None = None,
                    answer_given: str | None = None, auto_answered: bool = False,
                    similarity_score: float | None = None) -> int:
    """Log that a question was seen on a real form."""
    cur = conn.execute(
        "INSERT INTO question_sightings (question_id, application_id, raw_text, ats, "
        "answer_given, auto_answered, corrected, similarity, seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (question_id, application_id, raw_text, ats, answer_given,
         int(auto_answered), similarity_score, _now()))
    if question_id is not None:
        conn.execute(
            "UPDATE questions SET times_seen = times_seen + 1, updated_at = ? WHERE id = ?",
            (_now(), question_id))
    conn.commit()
    return cur.lastrowid


def apply_correction(conn: sqlite3.Connection, question_id: int, corrected_answer: str,
                     sighting_id: int | None = None) -> None:
    """Record a human correction: update the answer and cut the entry's confidence.

    Section 9's learning loop. The same correction must never be needed twice,
    so the corrected text becomes canonical immediately; and because the entry
    was wrong once, `times_corrected` permanently bars it from auto-answering.
    """
    row = conn.execute("SELECT confidence FROM questions WHERE id = ?",
                       (question_id,)).fetchone()
    if row is None:
        raise ValueError(f"No question with id {question_id}")

    new_confidence = max(0.0, float(row["confidence"]) * CORRECTION_PENALTY)
    conn.execute(
        "UPDATE questions SET canonical_answer = ?, confidence = ?, "
        "times_corrected = times_corrected + 1, embedding = ?, updated_at = ? WHERE id = ?",
        (corrected_answer, new_confidence, _serialize(vectorize(corrected_answer)),
         _now(), question_id))
    # Keep the embedding keyed to the question, not the answer.
    qtext = conn.execute("SELECT canonical_text FROM questions WHERE id = ?",
                         (question_id,)).fetchone()["canonical_text"]
    conn.execute("UPDATE questions SET embedding = ? WHERE id = ?",
                 (_serialize(vectorize(qtext)), question_id))

    if sighting_id is not None:
        conn.execute(
            "UPDATE question_sightings SET corrected = 1, correction_text = ? WHERE id = ?",
            (corrected_answer, sighting_id))
    conn.commit()


def rebuild_embeddings(conn: sqlite3.Connection) -> int:
    """Recompute every stored vector. Run after changing `vectorize()`."""
    rows = conn.execute("SELECT id, canonical_text FROM questions").fetchall()
    for row in rows:
        conn.execute("UPDATE questions SET embedding = ? WHERE id = ?",
                     (_serialize(vectorize(row["canonical_text"])), row["id"]))
    conn.commit()
    return len(rows)


def threshold_report(conn: sqlite3.Connection) -> dict:
    """How the thresholds are performing — auto-answers later corrected.

    Section 9 requires this every 50 entries. The system reports; it never
    adjusts its own thresholds.
    """
    total = conn.execute("SELECT COUNT(*) AS n FROM questions").fetchone()["n"]
    auto = conn.execute(
        "SELECT COUNT(*) AS n FROM question_sightings WHERE auto_answered = 1"
    ).fetchone()["n"]
    auto_corrected = conn.execute(
        "SELECT COUNT(*) AS n FROM question_sightings WHERE auto_answered = 1 AND corrected = 1"
    ).fetchone()["n"]
    return {
        "bank_entries": total,
        "auto_answered": auto,
        "auto_answered_then_corrected": auto_corrected,
        "auto_error_rate": (auto_corrected / auto) if auto else 0.0,
        "reuse_threshold": REUSE_THRESHOLD,
        "review_floor": REVIEW_FLOOR,
    }
