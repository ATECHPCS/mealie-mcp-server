"""Network-free duplicate-food detection for the Mealie food catalog.

Imported recipes mint a new food per unrecognised name, so a catalog quickly
grows near-duplicates: "plain Greek yogurt" vs "plain nonfat Greek yogurt",
"crushed red pepper flakes" vs "red pepper flakes", "hot water"/"warm water"
vs "water". This module finds those pairs so they can be merged via Mealie's
`PUT /api/foods/merge`.

It is deliberately conservative. Auto-merge only fires on a high similarity
score, and a curated KEEP_DISTINCT list protects real-but-similar foods whose
difference matters to a shopping list or macro count (bell-pepper colours,
ground-beef fat ratios, lemon vs lime). A separate, looser suggestion pass
surfaces borderline pairs for a human to eyeball — it never merges on its own.
"""

from __future__ import annotations

import difflib
import re
from typing import Dict, List, Optional, Tuple

# Score at/above which two names are treated as the same food for auto-merge.
AUTO_CUTOFF = 0.9
# Looser score for the human-review suggestion pass.
SUGGEST_CUTOFF = 0.82

# Hand-reviewed merges: minted name (lhs) should fold onto the canonical
# catalog food (rhs). Applied by exact normalised key.
MERGE_OVERRIDES: Dict[str, str] = {
    "dry white wine": "white wine",
    "plain greek yogurt": "plain nonfat greek yogurt",
    "avocado oil mayo": "avocado oil mayonnaise",
    "crushed red pepper flakes": "red pepper flakes",
    "fresh squeezed lemon juice": "fresh lemon juice",
    "hot water": "water",
    "warm water": "water",
    "near-boiling water": "water",
    "water in brewer": "water",
    "ice for containers": "ice cubes",
    "ice in krups carafe": "ice cubes",
}

# Pairs that look similar but are genuinely different foods — never auto-merge
# these, and suppress them from suggestions. Stored as frozenset pairs of
# normalised names.
KEEP_DISTINCT: List[frozenset] = [
    frozenset({"chicken breast", "chicken thighs"}),
    frozenset({"lemon juice", "lime juice"}),
    frozenset({"fresh lemon juice", "fresh lime juice"}),
    frozenset({"red bell pepper", "green bell pepper"}),
    frozenset({"red bell pepper", "yellow bell pepper"}),
    frozenset({"green bell pepper", "yellow bell pepper"}),
    frozenset({"red bell pepper", "bell pepper"}),
    frozenset({"green bell pepper", "bell pepper"}),
    frozenset({"85/15 ground beef", "ground beef"}),
    frozenset({"80/20 ground beef", "ground beef"}),
    frozenset({"light ranch dressing", "ranch dressing"}),
    frozenset({"spicy ranch dressing", "ranch dressing"}),
    frozenset({"pickle juice", "dill pickle juice"}),
    frozenset({"pickle juice", "pickling juice"}),
]

_KEEP_DISTINCT_LOOKUP = {p for p in KEEP_DISTINCT}


def _singularise(word: str) -> str:
    """Conservative, predictable de-pluralisation of one word.

    Deliberately narrow: it never mangles a word into a non-word. Leaves "ss",
    "us", "is" and "-ies" words untouched (so no "hummus"->"hummu",
    "cookies"->"cooky"); a missed singular/plural fold is harmless (it just
    shows up as a review suggestion) whereas a mangled key breaks exact matches.
    """
    if len(word) <= 3:
        return word
    if word.endswith(("ss", "us", "is", "ies")):
        return word  # glass, hummus, axis; leave berries/cookies alone
    if word.endswith("oes"):
        return word[:-2]  # tomatoes -> tomato, potatoes -> potato
    if word.endswith(("ches", "shes", "xes", "zes")):
        return word[:-2]  # peaches -> peach, dishes -> dish, boxes -> box
    if word.endswith("s"):
        return word[:-1]  # eggs -> egg, flakes -> flake
    return word


def norm(name: str) -> str:
    """Canonical key for a food name: lower-cased, de-punctuated, singularised,
    whitespace-collapsed. Keeps "/" so fat ratios (85/15) stay distinct."""
    s = (name or "").lower().strip()
    s = re.sub(r"[^\w\s/]", " ", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return " ".join(_singularise(w) for w in s.split())


# Build normalised lookups once norm() exists, so override/keep-distinct keys
# match norm()'s output (singularisation would otherwise leave e.g. "crushed
# red pepper flakes" unable to match the normalised "... flake").
_MERGE_OVERRIDES_NORM: Dict[str, str] = {
    norm(k): v for k, v in MERGE_OVERRIDES.items()
}
_KEEP_DISTINCT_NORM = {
    frozenset(norm(x) for x in pair) for pair in KEEP_DISTINCT
}


def _is_kept_distinct(a: str, b: str) -> bool:
    return frozenset({norm(a), norm(b)}) in _KEEP_DISTINCT_NORM


def resolve_existing(name: str, id_by_name: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """Exact/override-only resolution for the cleanup auto-path — NO fuzzy.

    Fuzzy similarity is catalog-corrupting for auto-apply ("salted butter" vs
    "unsalted butter" score 0.93), so the cleanup pipeline only reuses a food
    when the normalised name matches exactly or via a hand-reviewed override.
    Fuzzy matches live in `suggest_duplicate_clusters` for human review instead.

    Args:
        name: parsed food name.
        id_by_name: normalised-name -> food id (the catalog).

    Returns:
        (canonical_name, food_id) if an existing food should be reused, else
        (None, None). Unlike `find_duplicate`, an identical existing spelling
        IS returned (so cleanup reuses it rather than creating a duplicate).
    """
    key = norm(name)
    canonical = name
    override = _MERGE_OVERRIDES_NORM.get(key)
    if override is not None:
        canonical = override
        key = norm(override)
    fid = id_by_name.get(key)
    if fid is not None:
        return canonical, fid
    return None, None


def find_duplicate(
    name: str,
    catalog_names: List[str],
    cutoff: float = AUTO_CUTOFF,
) -> Optional[str]:
    """Return the catalog name `name` duplicates, or None.

    Order of resolution:
      1. curated MERGE_OVERRIDES (exact normalised key),
      2. exact normalised match against the catalog,
      3. fuzzy match at/above `cutoff`, excluding KEEP_DISTINCT pairs.
    The returned value is the original-cased catalog name.
    """
    key = norm(name)
    by_key: Dict[str, str] = {}
    for cn in catalog_names:
        by_key.setdefault(norm(cn), cn)

    override = _MERGE_OVERRIDES_NORM.get(key)
    if override is not None:
        # map onto the catalog's actual casing if present
        return by_key.get(norm(override), override)

    if key in by_key and by_key[key].lower() != (name or "").lower():
        return by_key[key]

    candidates = [k for k in by_key if k != key]
    matches = difflib.get_close_matches(key, candidates, n=3, cutoff=cutoff)
    for m in matches:
        cn = by_key[m]
        if _is_kept_distinct(name, cn):
            continue
        return cn
    return None


def suggest_duplicate_clusters(
    catalog_names: List[str],
    cutoff: float = SUGGEST_CUTOFF,
) -> List[Tuple[str, str, float]]:
    """Surface near-duplicate name pairs for human review.

    Returns a de-duplicated, sorted list of (name_a, name_b, score) with
    KEEP_DISTINCT pairs suppressed. Read-only — merges nothing.

    The score is symmetric — `max` of `ratio()` in both argument orders, since
    `difflib.SequenceMatcher.ratio()` is not order-invariant — so the result
    does not depend on catalog ordering.

    A naive all-pairs scan is O(n^2) and takes minutes on a multi-thousand-food
    catalog (past the MCP tool timeout). Two prefilters cut the work without
    changing the result. Both are necessary conditions for a symmetric
    `ratio() >= cutoff`: the number of matched characters `M` satisfies
    `M <= min(la, lb)` and `M <= common` (shared character-multiset count), so
    `ratio <= 2*min(la,lb)/(la+lb)` and `ratio <= 2*common/(la+lb)` in either
    order. Written multiplicatively (no division — so `cutoff=0` is safe and
    there is no float-boundary drop), with a small epsilon so an exact-boundary
    pair is kept for the real `ratio()` call to judge:

      * length window — with keys sorted by length (`la <= lb`),
        `2*la < cutoff*(la+lb)` can only become *more* true as `lb` grows, so we
        stop scanning outward once it holds.
      * character-multiset overlap — `2*common < cutoff*(la+lb)` rules a pair
        out before the expensive `ratio()` call.
    """
    key_to_name: Dict[str, str] = {}
    for n in catalog_names:
        key_to_name.setdefault(norm(n), n)

    from collections import Counter

    entries = sorted(
        ((k, len(k), Counter(k)) for k in key_to_name),
        key=lambda e: e[1],
    )
    eps = 1e-9

    out: List[Tuple[str, str, float]] = []
    for i, (ka, la, ca) in enumerate(entries):
        # No `la == 0` guard: an empty key is excluded at any real cutoff by the
        # length break below (2*0 < cutoff*lb), while cutoff=0 correctly pairs it.
        for kb, lb, cb in entries[i + 1:]:
            thresh = cutoff * (la + lb)
            if 2 * la < thresh - eps:
                break  # sorted by length — later kb only make this more true
            common = sum((ca & cb).values())
            if 2 * common < thresh - eps:
                continue  # not enough shared characters to reach the cutoff
            score = max(
                difflib.SequenceMatcher(None, ka, kb).ratio(),
                difflib.SequenceMatcher(None, kb, ka).ratio(),
            )
            if score < cutoff:
                continue
            na, nb = key_to_name[ka], key_to_name[kb]
            if _is_kept_distinct(na, nb):
                continue
            out.append((na, nb, round(score, 3)))
    out.sort(key=lambda t: t[2], reverse=True)
    return out
