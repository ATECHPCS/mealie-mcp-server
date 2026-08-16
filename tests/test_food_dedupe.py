"""Unit tests for network-free food-catalog dedupe."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mealie.food_dedupe import (  # noqa: E402
    find_duplicate,
    norm,
    resolve_existing,
    suggest_duplicate_clusters,
)


def test_singularise_never_mangles():
    # the old rule produced cooky/py/hummu; the new one must not
    assert norm("hummus") == "hummus"
    assert norm("cookies") == "cookies"
    assert norm("pies") == "pies"
    assert norm("tomatoes") == "tomato"
    assert norm("eggs") == "egg"
    assert norm("flakes") == "flake"


def test_resolve_existing_exact_and_override_only():
    idx = {norm("olive oil"): "oo-1", norm("water"): "w-1"}
    # exact (identical spelling) is reused — no duplicate creation
    assert resolve_existing("olive oil", idx) == ("olive oil", "oo-1")
    # override folds onto canonical even when singularised
    assert resolve_existing("crushed red pepper flakes", {norm("red pepper flakes"): "rpf-1"}) == (
        "red pepper flakes",
        "rpf-1",
    )
    # a genuinely new food resolves to nothing (created fresh downstream)
    assert resolve_existing("dragonfruit", idx) == (None, None)
    # fuzzy near-miss is NOT resolved (would corrupt the catalog)
    assert resolve_existing("salted butter", {norm("unsalted butter"): "ub-1"}) == (None, None)


def test_norm_singular_and_punctuation():
    assert norm("Eggs") == norm("egg")
    assert norm("Fresh, Lemon Juice!") == "fresh lemon juice"
    assert norm("85/15 ground beef") == "85/15 ground beef"  # ratio preserved


def test_override_folds_onto_canonical():
    catalog = ["water", "white wine", "plain nonfat Greek yogurt"]
    assert find_duplicate("hot water", catalog) == "water"
    assert find_duplicate("dry white wine", catalog) == "white wine"
    assert find_duplicate("plain Greek yogurt", catalog) == "plain nonfat Greek yogurt"


def test_exact_normalised_match():
    catalog = ["Chicken Thighs"]
    assert find_duplicate("chicken thigh", catalog) == "Chicken Thighs"


def test_fuzzy_match_above_cutoff():
    catalog = ["red pepper flakes"]
    # "crushed red pepper flakes" is in overrides, but a raw fuzzy case:
    assert find_duplicate("red pepper flake", catalog) == "red pepper flakes"


def test_keep_distinct_not_merged():
    catalog = ["chicken breast"]
    assert find_duplicate("chicken thighs", catalog) is None
    catalog = ["lemon juice"]
    assert find_duplicate("lime juice", catalog) is None
    catalog = ["red bell pepper"]
    assert find_duplicate("green bell pepper", catalog) is None


def test_no_false_positive_for_unrelated():
    catalog = ["olive oil", "chicken breast", "flour"]
    assert find_duplicate("brown sugar", catalog) is None


def test_identical_name_is_not_a_duplicate_of_itself():
    catalog = ["olive oil"]
    assert find_duplicate("olive oil", catalog) is None


def test_suggest_blocked_matches_naive_exactly():
    # the prefilters must not change results — compare against a brute-force
    # reference over a varied word list
    import difflib

    from mealie.food_dedupe import _is_kept_distinct, norm

    words = [
        "canned pear", "canned pea", "canned peach", "oat milk", "goat milk",
        "chicken breast", "chicken thighs", "chicken roast", "olive oil",
        "olive oyl", "red bell pepper", "green bell pepper", "bell pepper",
        "sugar", "sugars", "brown sugar", "raw sugar", "octopus", "octopuse",
        "water", "hot water", "sparkling water", "flour", "rice flour",
        "almond flour", "almond milk", "almond meal", "kosher salt", "sea salt",
        "table salt", "smoked paprika", "sweet paprika", "paprika",
    ]
    # include pathological asymmetric strings + exact-boundary lengths
    words = words + [
        "bacbcbacabbbacbab", "cbcbabcbcbbacbab",  # ratio differs by arg order
        "aa", "aaa",  # ratio exactly 0.8 at the length boundary
    ]

    def sym(a, b):
        return max(
            difflib.SequenceMatcher(None, a, b).ratio(),
            difflib.SequenceMatcher(None, b, a).ratio(),
        )

    for cutoff in (0.8, 0.82, 0.86, 0.9):
        key_to_name = {}
        for n in words:
            key_to_name.setdefault(norm(n), n)
        keys = list(key_to_name)
        naive = set()
        for i, ka in enumerate(keys):
            for kb in keys[i + 1:]:
                if sym(ka, kb) >= cutoff and not _is_kept_distinct(key_to_name[ka], key_to_name[kb]):
                    naive.add(frozenset({key_to_name[ka], key_to_name[kb]}))
        blocked = {frozenset({a, b}) for a, b, _ in suggest_duplicate_clusters(words, cutoff=cutoff)}
        assert blocked == naive, f"mismatch at cutoff {cutoff}: {blocked ^ naive}"


def test_suggest_cutoff_zero_does_not_crash():
    # cutoff=0 is documented; must not ZeroDivisionError (returns all pairs)
    pairs = suggest_duplicate_clusters(["a b", "a c", "d e"], cutoff=0)
    assert len(pairs) == 3  # every distinct pair qualifies at cutoff 0


def test_suggest_cutoff_zero_includes_empty_normalized_name():
    # a punctuation-only name normalizes to "" — at cutoff 0 it still pairs
    pairs = suggest_duplicate_clusters(["---", "a", "b"], cutoff=0)
    assert len(pairs) == 3  # every pair, including those with the empty key


def test_empty_normalized_name_excluded_at_real_cutoff():
    # but an empty key never spuriously matches at a normal cutoff
    pairs = suggest_duplicate_clusters(["---", "olive oil", "olive oyl"], cutoff=0.86)
    flat = {frozenset({a, b}) for a, b, _ in pairs}
    assert all("---" not in p for p in flat)


def test_suggest_surfaces_near_dupes_but_not_kept_distinct():
    catalog = [
        "plain Greek yogurt",
        "plain nonfat Greek yogurt",
        "chicken breast",
        "chicken thighs",  # KEEP_DISTINCT — must not appear
        "olive oil",
    ]
    pairs = suggest_duplicate_clusters(catalog)
    flat = {frozenset({a, b}) for a, b, _ in pairs}
    assert frozenset({"plain Greek yogurt", "plain nonfat Greek yogurt"}) in flat
    assert frozenset({"chicken breast", "chicken thighs"}) not in flat
