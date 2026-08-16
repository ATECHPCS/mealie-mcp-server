"""Network-free pre-processing for messy imported ingredient lines.

Mealie's own NLP parser (`/api/parser/ingredients`) is good at pulling a
quantity/unit/food out of a *simple* line ("2 lb boneless chicken thighs").
It has a handful of reliable failure modes on lines that scrapers routinely
produce, and those failures are what leave a recipe with raw, unstructured
ingredients:

  * "X or Y" alternatives            -> keeps X's food, mangles the rest
  * "salt and pepper, to taste"      -> one low-confidence blob, no food
  * "1 tsp each A, B, and C"         -> distributes one amount over 3 foods
  * "For the sauce:"                 -> a section header stored as a food
  * "12 slices ... Bread Recipe"     -> a sub-recipe reference, not a food
  * "32 fl oz (958 g) water ..."     -> chokes on parenthetical conversions

This module rewrites a raw line into zero or more *clean candidate lines* that
the NLP parser can handle, and classifies what kind of line it was so the
caller can decide whether the fix is safe to apply automatically or should be
held for a human. It performs the "subtract the quantity out and identify the
food" step for the specific patterns the NLP parser gets wrong, then defers the
actual quantity/unit/food extraction to Mealie's parser on the cleaned text.

Nothing here touches the network — it is pure string logic so it can be unit
tested against real scraped lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

# --- classification categories -------------------------------------------------

NORMAL = "normal"
SECTION_HEADER = "section_header"
RECIPE_REFERENCE = "recipe_reference"
DISTRIBUTIVE = "distributive"
COMPOUND = "compound"
PARENTHETICAL = "parenthetical"
ALTERNATIVE = "alternative"

# --- actions the orchestrator should take --------------------------------------

ACTION_PARSE = "parse"  # feed candidates to the NLP parser
ACTION_SECTION = "section_header"  # promote to a titled group / leave as note
ACTION_RECIPE_REF = "recipe_reference"  # leave raw, needs a manual sub-recipe link


# Single-word/short seasoning names that make "<a> and <b>" an idiomatic
# compound ("salt and pepper") rather than two genuinely separate foods
# ("beef and broccoli"). Only when BOTH sides are in this set do we split.
SEASONINGS = {
    "salt",
    "sea salt",
    "kosher salt",
    "garlic salt",
    "table salt",
    "fine salt",
    "flaky salt",
    "pepper",
    "black pepper",
    "white pepper",
    "ground black pepper",
    "freshly ground black pepper",
    "freshly cracked black pepper",
    "cracked black pepper",
    "coarse black pepper",
    "freshly ground pepper",
}

# Trailing prep/serving qualifiers that carry no food and should be lifted off
# into the note before classification (so "salt and pepper, to taste" is seen
# as the compound "salt and pepper").
_TRAILING_NOTE_RE = re.compile(
    r",?\s*(?:"
    r"to taste"
    r"|for (?:garnish|serving|serving\.?|toasting|coating[^,]*|finishing|the [^,]+)"
    r"|if needed"
    r"|optional"
    r"|divided"
    r"|plus more[^,]*"
    r")\s*$",
    re.IGNORECASE,
)

# A parenthetical that is *purely* a weight/volume/temperature conversion or a
# per-batch annotation — noise for food identification, safe to drop. This must
# match the ENTIRE parenthetical: a trailing food word ("12 oz frozen peas")
# means the food is inside the parentheses and the group must NOT be dropped.
_UNIT_TOKEN = (
    r"(?:g|kg|mg|ml|l|oz|fl\s?oz|lb|lbs?|gram|grams|kilograms?|ounces?|"
    r"pounds?|cups?|tbsp|tsp|°[cf]?)"
)
_MEASURE = r"[\d.,/\s]+\s*" + _UNIT_TOKEN

# Bare-word unit + measurement-qualifier vocabulary. If, after removing these,
# a parenthetical with a number still has alpha words left, the real food is
# probably inside it (e.g. "12 oz frozen peas" -> "frozen peas" remains).
_UNIT_WORDS = {
    "g", "kg", "mg", "ml", "l", "oz", "fl", "lb", "lbs", "gram", "grams",
    "kilogram", "kilograms", "ounce", "ounces", "pound", "pounds", "cup",
    "cups", "tbsp", "tsp", "tablespoon", "tablespoons", "teaspoon", "teaspoons",
}
_MEASURE_QUALIFIERS = {
    "about", "approx", "approximately", "roughly", "around", "each", "total",
    "per", "batch", "batches", "jar", "jars", "loaf", "loaves", "flesh",
    "drained", "packed", "sifted", "cooked", "raw", "uncooked", "diced",
    "chopped", "minced", "melted", "softened", "plus", "more", "or", "and",
    "the", "of", "a", "to", "weight", "net", "dry", "before", "after",
}
_AMOUNT_PAREN_RE = re.compile(
    r"^\s*(?:about\s+|approx\.?\s+)?"
    + _MEASURE
    + r"(?:\s*[/,]\s*" + _MEASURE + r")*"  # "/ 320 g", ", 479 g"
    + r"(?:\s+per\s+[\w\s]+?)?"  # "per batch", "per jar"
    + r"\s*$",
    re.IGNORECASE,
)

# Trailing "... about 200°F/93°C ..." temperature annotations left inline.
_TEMP_TAIL_RE = re.compile(
    r",?\s*(?:about\s+)?\d[\d.,/\s]*°?\s?[cf]\b.*$", re.IGNORECASE
)


@dataclass
class Candidate:
    """A cleaned line to hand to the NLP parser, plus any note fragment lifted
    off the original that should be preserved on the resulting ingredient."""

    text: str
    note_extra: str = ""

    def as_dict(self) -> dict:
        return {"text": self.text, "note_extra": self.note_extra}


@dataclass
class CleanedLine:
    raw: str
    category: str
    action: str
    auto_safe: bool
    candidates: List[Candidate] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "raw": self.raw,
            "category": self.category,
            "action": self.action,
            "auto_safe": self.auto_safe,
            "candidates": [c.as_dict() for c in self.candidates],
            "reason": self.reason,
        }


def _norm_ws(text: str) -> str:
    return re.sub(r"\s{2,}", " ", text).strip().strip(",").strip()


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def _paren_hides_food(inner: str) -> bool:
    """True if a parenthetical carries a quantity AND a non-measure word — a
    sign the real food is inside it ("12 oz frozen peas"), as opposed to a pure
    qualifier ("about 150 g flesh")."""
    if not _has_digit(inner):
        return False
    words = re.findall(r"[a-z]+", inner.lower())
    leftover = [w for w in words if w not in _UNIT_WORDS and w not in _MEASURE_QUALIFIERS]
    return bool(leftover)


def is_section_header(raw: str) -> bool:
    """A section label stored as an ingredient, e.g. "For the sauce:" or
    "For the pineapple chimichurri". No quantity, no real food."""
    s = raw.strip()
    if re.match(r"^for\s+the\b", s, re.IGNORECASE) and not _has_digit(s):
        return True
    # A short, digit-free line ending in a colon ("Toppings:")
    if s.endswith(":") and not _has_digit(s) and len(s.split()) <= 6:
        return True
    return False


def is_recipe_reference(raw: str, known_titles: Optional[set] = None) -> bool:
    """A line that points at another recipe rather than naming a food, e.g.
    "12 slices Keto Brioche Bread Recipe (...)". These must never be written
    back as a food — linking a sub-recipe is a UI action.

    Detection needs explicit evidence and is deliberately narrow to avoid
    misclassifying ordinary foods:
      * the word "recipe" appears, or
      * the line has NO quantity and its whole text exactly equals a known
        recipe title. A line *with* a quantity ("2 cups tomato sauce") is an
        ingredient measurement, not a sub-recipe link, even if a "Tomato Sauce"
        recipe happens to exist.
    """
    if re.search(r"\brecipes?\b", raw, re.IGNORECASE):
        return True
    if known_titles and not _has_digit(raw):
        core = raw.strip().rstrip(",.").lower()
        titles = {t.strip().lower() for t in known_titles if t and len(t) > 6}
        if core in titles:
            return True
    return False


def _extract_parentheticals(text: str) -> tuple[str, List[str], bool, bool]:
    """Pull every "(...)" group out of `text`.

    Returns (stripped_text, note_fragments, saw_alternative, suspicious).
    Pure amount/conversion parentheticals are dropped; "(or olive oil)" becomes
    an alternative note fragment; anything else is preserved as a note fragment.
    `suspicious` is True when a group carries a quantity but is *not* a pure
    measure (e.g. "12 oz frozen peas") — a sign the real food is inside the
    parentheses, so the line must not be auto-applied.
    """
    notes: List[str] = []
    saw_alt = False
    suspicious = False

    def _repl(match: re.Match) -> str:
        nonlocal saw_alt, suspicious
        inner = match.group(1).strip()
        low = inner.lower()
        if low.startswith("or ") or low == "or":
            saw_alt = True
            notes.append(inner)
        elif _AMOUNT_PAREN_RE.match(inner):
            pass  # pure conversion/measure noise — drop it
        elif inner:
            if _paren_hides_food(inner):
                suspicious = True
            notes.append(inner)
        return " "

    stripped = re.sub(r"\(([^)]*)\)", _repl, text)
    return _norm_ws(stripped), notes, saw_alt, suspicious


def _split_items(items_text: str) -> List[str]:
    """Split a distributive item list into its foods.

    "salt, pepper, Italian seasoning, and onion powder" -> 4 items. Only a
    *leading* "and"/"& " on the final comma-separated item is treated as a list
    joiner, so an internal conjunction in a compound food name survives:
    "salt, macaroni and cheese, pepper" -> ["salt", "macaroni and cheese",
    "pepper"]. A comma-free "A and B" is split on its single conjunction.
    """
    parts = [p.strip() for p in items_text.split(",") if p.strip()]
    if len(parts) > 1:
        parts[-1] = re.sub(r"^(?:and|&)\s+", "", parts[-1], flags=re.IGNORECASE)
        return [p for p in parts if p]
    # no commas: a single "A and B" list
    halves = re.split(r"\s+(?:and|&)\s+", items_text, maxsplit=1, flags=re.IGNORECASE)
    return [h.strip() for h in halves if h.strip()]


def _try_distributive(text: str, base_note: str) -> Optional[List[Candidate]]:
    """"1 tsp each garlic powder and smoked paprika" -> one candidate per food,
    each carrying the shared "1 tsp" amount."""
    m = re.match(r"^(?P<amt>.+?)\s+each\s+(?P<items>.+)$", text, re.IGNORECASE)
    if not m:
        return None
    amt = m.group("amt").strip()
    if not _has_digit(amt):
        return None
    items = _split_items(m.group("items"))
    if len(items) < 2:
        return None
    return [Candidate(text=f"{amt} {item}", note_extra=base_note) for item in items]


def _try_compound_seasoning(text: str, base_note: str) -> Optional[List[Candidate]]:
    """"Salt and black pepper" -> ["salt", "black pepper"], but only when both
    halves are known seasonings (never "chicken and rice")."""
    m = re.match(r"^(?P<a>[\w\s]+?)\s+and\s+(?P<b>[\w\s]+?)$", text, re.IGNORECASE)
    if not m:
        return None
    a = _norm_ws(m.group("a")).lower()
    b = _norm_ws(m.group("b")).lower()
    if a in SEASONINGS and b in SEASONINGS:
        return [
            Candidate(text=m.group("a").strip(), note_extra=base_note),
            Candidate(text=m.group("b").strip(), note_extra=base_note),
        ]
    return None


def _try_alternative(text: str, base_note: str) -> Optional[List[Candidate]]:
    """"2 lb sirloin or flank steak" -> primary candidate "2 lb sirloin", with
    "or flank steak" preserved as a note. Held for review by the caller: which
    alternative to keep is a judgement call, and the split can drop a shared
    head noun ("avocado or olive oil")."""
    m = re.search(r"\s+or\s+", text, re.IGNORECASE)
    if not m:
        return None
    primary = _norm_ws(text[: m.start()])
    alt = _norm_ws(text[m.end():])
    if not primary or not alt:
        return None
    note = _join_notes(f"or {alt}", base_note)
    return [Candidate(text=primary, note_extra=note)]


def _join_notes(*parts: str) -> str:
    return "; ".join(p.strip() for p in parts if p and p.strip())


def clean_line(raw: str, known_titles: Optional[set] = None) -> CleanedLine:
    """Classify and normalise a single raw ingredient line.

    The returned CleanedLine says what kind of line it is, whether the fix is
    safe to apply without human review, and the cleaned candidate line(s) to
    feed the NLP parser.
    """
    raw = (raw or "").strip()
    if not raw:
        return CleanedLine(raw, NORMAL, ACTION_PARSE, False, [], "empty line")

    # A labeled line — "For the glaze: 3 tbsp butter" — is a real ingredient
    # with a section label glued on. Strip the label and parse the remainder;
    # a bare "For the glaze:" (no remainder) falls through to section-header.
    label_m = re.match(r"^for\s+(?:the\s+)?[^:]{2,60}:\s*(?P<rest>\S.*)$", raw, re.I)
    work = label_m.group("rest").strip() if label_m else raw

    if is_section_header(work):
        return CleanedLine(
            raw, SECTION_HEADER, ACTION_SECTION, True, [],
            "section header — promote to a titled group, not a food",
        )

    if is_recipe_reference(work, known_titles):
        return CleanedLine(
            raw, RECIPE_REFERENCE, ACTION_RECIPE_REF, False, [],
            "references another recipe — link the sub-recipe in the UI",
        )

    # Strip parenthetical conversions / lift "(or ...)" alternatives out.
    stripped, paren_notes, saw_alt_paren, suspicious_paren = _extract_parentheticals(work)
    stripped = _TEMP_TAIL_RE.sub("", stripped)
    stripped = _norm_ws(stripped)

    # Lift a trailing "to taste"/"for garnish"/... qualifier off.
    trailing = ""
    tm = _TRAILING_NOTE_RE.search(stripped)
    if tm:
        trailing = tm.group(0).lstrip(", ").strip()
        stripped = _norm_ws(stripped[: tm.start()])

    base_note = _join_notes(*paren_notes, trailing)
    # A food-bearing parenthetical ("12 oz frozen peas") means the stripped text
    # is not trustworthy — never auto-apply such a line.
    auto = not suspicious_paren

    # 1) distributive "N unit each A, B, C"
    dist = _try_distributive(stripped, base_note)
    if dist:
        return CleanedLine(
            raw, DISTRIBUTIVE, ACTION_PARSE, auto, dist,
            "one amount distributed over several foods — split per food",
        )

    # 2) idiomatic seasoning compound "salt and pepper"
    comp = _try_compound_seasoning(stripped, base_note)
    if comp:
        return CleanedLine(
            raw, COMPOUND, ACTION_PARSE, auto, comp,
            "seasoning compound — split into separate foods",
        )

    # 3) "X or Y" alternative (inline). Parenthetical "(or Y)" was already
    #    lifted into base_note above, so a line with only a paren-alternative
    #    is a clean single food and stays auto-safe.
    alt = _try_alternative(stripped, base_note)
    if alt:
        return CleanedLine(
            raw, ALTERNATIVE, ACTION_PARSE, False, alt,
            "alternative ingredients — keep the first, note the rest (review)",
        )

    # 4) parenthetical-only cleanup (incl. a lifted "(or Y)" alternative)
    if paren_notes or saw_alt_paren or (tm is not None):
        reason = "stripped parenthetical/measure noise"
        if suspicious_paren:
            reason = "parenthetical carries a quantity + words — food may be inside (review)"
        elif saw_alt_paren:
            reason = "parenthetical alternative lifted to note — first food kept"
        return CleanedLine(
            raw, PARENTHETICAL, ACTION_PARSE, auto,
            [Candidate(text=stripped, note_extra=base_note)], reason,
        )

    # 5) ordinary line — hand straight to the parser
    return CleanedLine(
        raw, NORMAL, ACTION_PARSE, auto,
        [Candidate(text=stripped or raw, note_extra=base_note)],
        "ordinary line",
    )
