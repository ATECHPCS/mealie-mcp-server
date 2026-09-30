"""Duplicate-recipe detection on import."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mealie.recipe_dedupe import find_duplicates, ingredients_match, normalize_url  # noqa: E402

LIB = [
    {"slug": "lemon-garlic-shrimp", "name": "Lemon Garlic Shrimp",
     "orgURL": "https://www.example.com/shrimp/?utm_source=x#recipe"},
    {"slug": "shrimp", "name": "Shrimp", "orgURL": None},
    {"slug": "a", "name": "Keto Waffles", "orgURL": "https://www.facebook.com/saved"},
    {"slug": "b", "name": "Taco Bowl", "orgURL": "https://www.facebook.com/saved"},
]


def test_normalize_url():
    assert normalize_url("https://www.Example.com/shrimp/?utm=1#r") == "example.com/shrimp"
    assert normalize_url("example.com/shrimp") == "example.com/shrimp"
    assert normalize_url("") is None


def test_same_url_matches_ignoring_www_query_fragment():
    d = find_duplicates(LIB, url="http://example.com/shrimp")
    assert [x["slug"] for x in d] == ["lemon-garlic-shrimp"]
    assert d[0]["reason"] == "same source URL"


def test_shared_generic_url_is_ignored():
    assert find_duplicates(LIB, url="https://facebook.com/saved") == []


def test_name_matches_order_plural_filler_and_spelling():
    for name in ["Garlic-Lemon Shrimp", "Easy Lemon Garlic Shrimps", "Lemon Garlik Shrimp"]:
        assert [x["slug"] for x in find_duplicates(LIB, name=name)] == ["lemon-garlic-shrimp"], name


def test_parenthetical_and_numbers_ignored():
    lib = [{"slug": "w", "name": "Publix Sweet & Spicy Chili Boneless Chicken Wings (10 pc)"}]
    d = find_duplicates(lib, name="Publix Sweet & Spicy Chili Boneless Chicken Wings")
    assert d and d[0]["reason"] == "same name"


def test_contained_name_with_three_words_asks():
    lib = [{"slug": "l", "name": "Lemon Garlic Butter Shrimp"}]
    d = find_duplicates(lib, name="Garlic Butter Shrimp")
    assert d and d[0]["reason"] == "very similar name"
    assert find_duplicates(lib, name="Butter Shrimp") == []  # only 2 words


def test_different_dish_is_not_a_duplicate():
    assert find_duplicates(LIB, name="Shrimp Tacos") == []
    assert find_duplicates(LIB, name="Garlic Butter Shrimp") == []


def test_new_recipe_excluded_from_its_own_check():
    assert find_duplicates(LIB, name="Shrimp", exclude_slug="shrimp") == []


def _r(*foods):
    return {"recipeIngredient": [{"food": {"name": f}} for f in foods]}


def test_ingredients_match_thresholds():
    keto = _r("tuna", "egg", "keto bun", "sugar-free honey", "scallion", "garlic", "soy sauce")
    regular = _r("tuna", "egg", "brioche bun", "honey", "scallions", "garlic", "soy sauce")
    assert ingredients_match(keto, regular)["match"]  # 5 of 7 shared, plural-insensitive
    assert not ingredients_match(_r("a", "b", "c", "d"), _r("a", "b", "x", "y"))["match"]  # 50%
    assert not ingredients_match(_r("water", "coffee"), _r("water", "coffee", "milk"))["match"]
    assert ingredients_match(_r("water", "coffee", "milk"), _r("water", "coffee", "milk"))["match"]
    assert ingredients_match({}, _r("a"))["match"]  # can't judge -> ask


def test_raw_lines_compare_by_food_not_amount():
    raw = {"recipeIngredient": [{"food": None, "note": n} for n in
                                ["1 cup almond flour", "2 large eggs", "1 tsp baking powder", "½ tsp salt"]]}
    parsed = _r("almond flour", "egg", "baking powder", "salt")
    assert ingredients_match(raw, parsed)["match"]
