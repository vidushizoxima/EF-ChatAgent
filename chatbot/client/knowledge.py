"""
knowledge.py — the local knowledge base, lifted from the EF Voice project.

`knowledge/*.md` is a straight copy of `EurekaForbesVoice/knowledge/`. It is the
only source of a product price, an AMC inclusion, or what the loyalty offer covers.
Facts live there, behaviour lives in the prompt.

Sections are split on `##` headings and matched by keyword. No embeddings, no vector
store: the corpus is six files, and a scored keyword match over ~40 sections is both
faster and easier to debug than a similarity search nobody can explain.

Two things to know about the source material:

1. **It was written to be spoken.** Prices are words — "nine thousand four hundred
   ninety nine rupees" — because a voice agent reads them aloud. WhatsApp wants
   digits, so `to_digits()` converts them and the tool returns both forms.
2. **Not every file is live.** `offer-flat-ten-percent.md` is a different, earlier
   campaign. Loading it beside the current twenty percent offer would hand the agent
   two contradictory discounts, so it is excluded by name.
"""

import logging
import os
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

KNOWLEDGE_DIR = os.getenv(
    "KNOWLEDGE_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "knowledge"),
)

# Campaigns that are not running. Excluded so the agent cannot quote a dead offer.
EXCLUDED_FILES = {"README.md", "offer-flat-ten-percent.md"}

# Words too common to discriminate between sections.
STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "it", "of", "and", "or", "to", "in", "on",
    "for", "with", "what", "how", "much", "does", "do", "i", "my", "me", "you", "your",
    "can", "will", "that", "this", "there", "any", "be", "at", "if", "so", "but",
    "hai", "ka", "ki", "ke", "mera", "meri", "kya", "hoon", "se",
}

_SECTIONS: Optional[List[Dict]] = None

# Spoken-number vocabulary, largest first so "hundred" does not eat "thousand".
_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_MULTIPLIERS = {"hundred": 100, "thousand": 1000, "lakh": 100000, "crore": 10000000}


def _spoken_to_int(phrase: str) -> Optional[int]:
    total, current = 0, 0
    seen = False
    for word in phrase.replace("-", " ").split():
        w = word.lower().strip(",")
        if w in _NUM_WORDS:
            current += _NUM_WORDS[w]
            seen = True
        elif w in _MULTIPLIERS:
            mult = _MULTIPLIERS[w]
            if mult >= 1000:
                total += max(current, 1) * mult
                current = 0
            else:
                current = max(current, 1) * mult
            seen = True
        elif w == "and":
            continue
        else:
            return None
    return (total + current) if seen else None


_PRICE_RE = re.compile(
    r"\b((?:(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
    r"sixty|seventy|eighty|ninety|hundred|thousand|lakh|crore|and)[\s-]+)+)rupees\b",
    re.IGNORECASE,
)


def to_digits(text: str) -> str:
    """'nine thousand four hundred ninety nine rupees' -> 'Rs 9,499'.

    The knowledge base is written for a voice agent. Spelled-out prices read as
    padding in a chat message, and a customer cannot scan them.
    """
    def swap(match):
        value = _spoken_to_int(match.group(1))
        return f"Rs {value:,}" if value else match.group(0)

    return _PRICE_RE.sub(swap, text)


def _load() -> List[Dict]:
    global _SECTIONS
    if _SECTIONS is not None:
        return _SECTIONS

    sections: List[Dict] = []
    if not os.path.isdir(KNOWLEDGE_DIR):
        logger.warning(f"No knowledge directory at {KNOWLEDGE_DIR}")
        _SECTIONS = sections
        return sections

    for filename in sorted(os.listdir(KNOWLEDGE_DIR)):
        if not filename.endswith(".md") or filename in EXCLUDED_FILES:
            continue
        path = os.path.join(KNOWLEDGE_DIR, filename)
        try:
            raw = open(path, encoding="utf-8").read()
        except OSError as e:
            logger.warning(f"Could not read {filename}: {e}")
            continue

        # The <!-- --> blocks are provenance notes for whoever maintains the file,
        # not content, and they mention prices that were deliberately removed.
        raw = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)

        for chunk in re.split(r"^##\s+", raw, flags=re.M)[1:]:
            lines = chunk.strip().split("\n", 1)
            heading = lines[0].strip()
            body = (lines[1] if len(lines) > 1 else "").strip()
            if not body:
                continue
            sections.append({
                "source": filename,
                "heading": heading,
                "body": body,
                "haystack": f"{heading} {body}".lower(),
            })

    _SECTIONS = sections
    logger.info(f"📚 Knowledge base: {len(sections)} sections from {KNOWLEDGE_DIR}")
    return sections


def reload() -> int:
    global _SECTIONS
    _SECTIONS = None
    return len(_load())


def sections() -> List[Dict]:
    return _load()


def search(question: str, limit: int = 3) -> List[Dict]:
    """Best-matching sections for a question, most relevant first."""
    terms = [t for t in re.findall(r"[a-z]+", (question or "").lower())
             if t not in STOPWORDS and len(t) > 2]
    if not terms:
        return []

    scored = []
    for section in _load():
        hay = section["haystack"]
        score = 0
        for term in terms:
            if term in section["heading"].lower():
                score += 3          # a heading match is what the file is *about*
            elif term in hay:
                score += 1
        if score:
            scored.append((score, section))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [s for _, s in scored[:limit]]
