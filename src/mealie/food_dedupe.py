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
    """Conservative, predictable de-pluralisation of one word."""
    if len(word) <= 3:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"  # berries -> berry
    if word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]  # peaches -> peach, glasses -> glass
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]  # eggs -> egg, but keep "molass"/"hummus"-ish "ss"
    return word


def norm(name: str) -> str:
    """Canonical key for a food name: lower-cased, de-punctuated, singularised,
    whitespace-collapsed. Keeps "/" so fat ratios (85/15) stay distinct."""
    s = (name or "").lower().strip()
    s = re.sub(r"[^\w\s/]", " ", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return " ".join(_singularise(w) for w in s.split())


def _is_kept_distinct(a: str, b: str) -> bool:
    return frozenset({a, b}) in _KEEP_DISTINCT_LOOKUP or frozenset(
        {norm(a), norm(b)}
    ) in _KEEP_DISTINCT_LOOKUP


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

    override = MERGE_OVERRIDES.get(key)
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
    """
    keys = [norm(n) for n in catalog_names]
    key_to_name: Dict[str, str] = {}
    for n, k in zip(catalog_names, keys):
        key_to_name.setdefault(k, n)
    unique_keys = list(key_to_name)

    seen: set = set()
    out: List[Tuple[str, str, float]] = []
    for i, ka in enumerate(unique_keys):
        for kb in unique_keys[i + 1:]:
            score = difflib.SequenceMatcher(None, ka, kb).ratio()
            if score < cutoff:
                continue
            na, nb = key_to_name[ka], key_to_name[kb]
            if _is_kept_distinct(na, nb):
                continue
            pair = frozenset({ka, kb})
            if pair in seen:
                continue
            seen.add(pair)
            out.append((na, nb, round(score, 3)))
    out.sort(key=lambda t: t[2], reverse=True)
    return out
