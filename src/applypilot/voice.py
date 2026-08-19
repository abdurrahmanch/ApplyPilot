"""Voice profile loading and batch sameness checking.

## Voice

Abdur-Rahman's writing voice lives outside this repo, in a two-part system he
already maintains: a read-only core (`voice-profile.md`, built from his own
rewrites) plus per-channel registers that agents append learnings to. The
tailor and cover stages load core + the job-applications register so generated
documents sound like him rather than like a model.

The core is never edited by anything here. Corrections from the review gate
append to the register only.

## Sameness

Seventeen cover letters sharing a skeleton with the company name swapped is the
failure mode that gets applications binned, and it is invisible inside any one
letter — only the batch shows it. `check_batch_sameness` compares every pair
and flags both members of any pair that is too alike, so the review gate can
surface them together.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Default location; override with VOICE_PROFILE_DIR.
DEFAULT_VOICE_DIR = Path.home() / "Documents" / "Repositories" / "voice-profile"
REGISTER_NAME = "job-applications.md"

# Pairwise trigram similarity above this flags both letters in a batch.
# Deliberately low: two letters for genuinely similar backend roles will share
# vocabulary, so this catches structural cloning, not topical overlap.
SAMENESS_THRESHOLD = 0.55

_WORD_RE = re.compile(r"[a-z0-9']+")

# Openings and closings are formulaic by design and would dominate any
# similarity score, so the comparison ignores them.
_BOILERPLATE = re.compile(
    r"^\s*dear[^\n]*\n|sincerely[^\n]*$|best regards[^\n]*$", re.IGNORECASE | re.MULTILINE)


def voice_dir() -> Path:
    return Path(os.environ.get("VOICE_PROFILE_DIR", str(DEFAULT_VOICE_DIR)))


def load_voice_profile(register: str = REGISTER_NAME) -> str:
    """Return the core profile plus one channel register, ready to prompt with.

    Returns an empty string when the profile is not on this machine. The
    caller decides whether that is fatal; generation without it produces
    generic copy rather than wrong copy.
    """
    base = voice_dir()
    core_path = base / "voice-profile.md"
    register_path = base / "registers" / register

    parts: list[str] = []
    if core_path.exists():
        parts.append(core_path.read_text(encoding="utf-8"))
    else:
        log.warning("Voice profile core not found at %s", core_path)
    if register_path.exists():
        parts.append(register_path.read_text(encoding="utf-8"))
    else:
        log.warning("Voice register not found at %s", register_path)

    return "\n\n---\n\n".join(parts)


def voice_profile_available() -> bool:
    return (voice_dir() / "voice-profile.md").exists()


def append_learning(text: str, register: str = REGISTER_NAME) -> bool:
    """Append one learned correction to the register. Never touches the core."""
    path = voice_dir() / "registers" / register
    if not path.exists():
        log.warning("Cannot append learning; register missing at %s", path)
        return False
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n" + text.rstrip() + "\n")
    return True


# ── Batch sameness ────────────────────────────────────────────────────────

def _trigrams(text: str) -> set[tuple[str, str, str]]:
    """Word trigrams, with salutation/sign-off boilerplate removed."""
    stripped = _BOILERPLATE.sub(" ", text.lower())
    words = _WORD_RE.findall(stripped)
    return {tuple(words[i:i + 3]) for i in range(len(words) - 2)}


def letter_similarity(a: str, b: str) -> float:
    """Jaccard similarity over word trigrams, in [0, 1].

    Trigrams rather than characters: cloning shows up as shared phrasing, and
    swapping one company name barely moves a trigram set.
    """
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    union = ta | tb
    return len(ta & tb) / len(union) if union else 0.0


def check_batch_sameness(letters: dict[str, str],
                         threshold: float = SAMENESS_THRESHOLD) -> list[dict]:
    """Compare every pair of letters in a batch.

    Args:
        letters: {application_id: letter_text}
        threshold: similarity at or above which a pair is flagged.

    Returns one entry per offending pair, sorted worst first. BOTH members are
    named so the review gate can show them side by side — neither letter is at
    fault on its own.
    """
    ids = sorted(letters)
    flagged: list[dict] = []
    for i, a_id in enumerate(ids):
        for b_id in ids[i + 1:]:
            score = letter_similarity(letters[a_id], letters[b_id])
            if score >= threshold:
                flagged.append({"a": a_id, "b": b_id, "similarity": round(score, 3)})
    flagged.sort(key=lambda f: -f["similarity"])
    return flagged


def repeated_phrases(text: str, n: int = 2, min_repeats: int = 3) -> list[str]:
    """Phrases repeated inside a single letter — the within-letter padding tell.

    The batch check catches letters cloned from each other; this catches one
    letter padding itself, which is what a high word target produces.

    Word pairs, not triples: the real 2026-08-19 example was "responsive
    design" five times, which never repeats as a trigram because the
    surrounding words differ every time.
    """
    words = _WORD_RE.findall(_BOILERPLATE.sub(" ", text.lower()))
    counts: dict[str, int] = {}
    for i in range(len(words) - n + 1):
        phrase = " ".join(words[i:i + n])
        counts[phrase] = counts.get(phrase, 0) + 1
    # Drop phrases that are entirely filler words; they repeat harmlessly.
    filler = {"the", "a", "an", "and", "of", "to", "in", "for", "with", "on",
              "at", "i", "my", "you", "your", "that", "this", "is", "are", "have"}
    return sorted(
        phrase for phrase, count in counts.items()
        if count >= min_repeats and not all(w in filler for w in phrase.split())
    )
