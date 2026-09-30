"""Spot an import that duplicates a recipe already in Mealie.

Two signals:

- **Same source URL**, compared without scheme, ``www.``, tracking
  parameters (``utm_*``, ``fbclid``, ``si``...), fragment or trailing slash.
  Other query parameters are kept: ``youtube.com/watch?v=A`` and ``?v=B`` are
  different videos. A URL that two or more existing recipes already
  share (e.g. ``facebook.com/saved``) identifies a page, not a recipe, and is
  ignored.
- **Same name**: equal after lowercasing, dropping punctuation, parenthetical
  asides and bare numbers ("(10 pc)"), making words singular, ignoring word
  order and a few filler words ("easy", "best", "recipe"...), or a spelling
  variant of the same length. "Lemon Garlic Shrimp" matches "Garlic-Lemon
  Shrimp"; "Shrimp Tacos" does not match "Shrimp".
- **Very similar name**: one name's words all appear in the other and the
  shorter has at least 3 words ("Garlic Butter Shrimp" / "Lemon Garlic Butter
  Shrimp"). AI imports often trim or embellish a title.

A name match alone is weak — this library has keto/regular variants and several
unrelated recipes with overlapping titles — so callers confirm it with
``ingredients_match``: the recipes must share at least 60% of their ingredients
(by food), and at least 4 of them (all of them for shorter recipes).
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit

FILLER_WORDS = frozenset({
    "easy", "best", "simple", "quick", "homemade", "recipe", "the", "a", "an",
    "my", "perfect", "ultimate", "delicious", "copycat",
})
SPELLING_RATIO = 0.92
MIN_CONTAINED_WORDS = 3
MIN_SHARED_RATIO = 0.6
MIN_SHARED_FOODS = 4
# leading words dropped from an unparsed ingredient line before comparing it
_AMOUNT_WORDS = frozenset({
    "cup", "tablespoon", "tbsp", "teaspoon", "tsp", "oz", "ounce", "lb", "pound",
    "g", "gram", "kg", "kilogram", "ml", "milliliter", "l", "liter", "clove",
    "pinch", "dash", "can", "slice", "stick", "package", "pkg", "bunch", "handful",
    "large", "small", "medium", "of", "a", "an", "to", "and", "or",
})


_TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "dclid", "msclkid", "igshid", "igsh", "si", "mc_cid",
    "mc_eid", "ref", "ref_src", "feature", "mibextid", "rdid", "share_url",
    "_hsenc", "_hsmi", "sfnsn",
})


def normalize_url(url: Optional[str]) -> Optional[str]:
    if not url or not url.strip():
        return None
    parts = urlsplit(url.strip() if "://" in url else f"https://{url.strip()}")
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    path = parts.path.rstrip("/")
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    )
    return f"{host}{path}" + (f"?{urlencode(query)}" if query else "")


def _words(text: Optional[str]) -> List[str]:
    """Casefolded, accent-folded words; non-Latin scripts are kept."""
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    folded = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.findall(r"[^\W_]+", folded)


def _singular(word: str) -> str:
    if len(word) <= 3 or word.endswith("ss"):
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("oes") or re.search(r"(ch|sh|x|ss)es$", word):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def name_key(name: Optional[str]) -> frozenset:
    text = re.sub(r"\([^)]*\)", " ", name or "")
    words = [_singular(w) for w in _words(text) if not w.isdigit()]
    core = [w for w in words if w not in FILLER_WORDS]
    return frozenset(core or words)


def _same_name(a: str, b: str) -> Optional[str]:
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return None
    if ka == kb:
        return "same name"
    ja, jb = " ".join(sorted(ka)), " ".join(sorted(kb))
    if len(ka) == len(kb) and min(len(ja), len(jb)) >= 6:
        if difflib.SequenceMatcher(None, ja, jb).ratio() >= SPELLING_RATIO:
            return "spelling variant of the name"
    small, big = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    if len(small) >= MIN_CONTAINED_WORDS and small < big:
        return "very similar name"
    return None


def find_duplicates(
    recipes: Iterable[Dict[str, Any]],
    name: Optional[str] = None,
    url: Optional[str] = None,
    exclude_slug: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Existing recipes that ``name``/``url`` duplicates, each with a reason."""
    recipes = [r for r in recipes if r.get("slug") != exclude_slug]
    url_counts = Counter(normalize_url(r.get("orgURL")) for r in recipes)
    want_url = normalize_url(url)
    out: List[Dict[str, Any]] = []
    for r in recipes:
        reason = None
        r_url = normalize_url(r.get("orgURL"))
        if want_url and r_url == want_url and url_counts[r_url] == 1:
            reason = "same source URL"
        elif name:
            reason = _same_name(name, r.get("name") or "")
        if reason:
            out.append({"slug": r.get("slug"), "name": r.get("name"),
                        "source_url": r.get("orgURL"), "reason": reason})
    return out


def food_keys(recipe: Dict[str, Any]) -> set:
    """Normalized food names of a recipe's ingredients (note text if unparsed)."""
    out = set()
    for ing in recipe.get("recipeIngredient") or []:
        food = (ing.get("food") or {}).get("name")
        words = [_singular(w) for w in _words(food or ing.get("note")) if not w.isdigit()]
        if not food:  # raw line: drop the leading amount/unit words
            while words and words[0] in _AMOUNT_WORDS:
                words.pop(0)
        if words:
            out.add(" ".join(words))
    return out


def ingredients_match(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Whether two recipes share enough ingredients to be the same recipe.

    Recipes with no readable ingredients can't be judged: that is ``match:
    False, unknown: True`` — never evidence for deleting anything.
    """
    fa, fb = food_keys(a), food_keys(b)
    if not fa or not fb:
        return {"match": False, "unknown": True, "shared": 0,
                "of": max(len(fa), len(fb)), "ratio": None}
    shared, biggest = len(fa & fb), max(len(fa), len(fb))
    ratio = shared / biggest
    needed = min(MIN_SHARED_FOODS, biggest)
    return {"match": ratio >= MIN_SHARED_RATIO and shared >= needed, "unknown": False,
            "shared": shared, "of": biggest, "ratio": round(ratio, 2)}
