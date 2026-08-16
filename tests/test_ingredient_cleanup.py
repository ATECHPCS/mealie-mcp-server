"""Unit tests for the network-free ingredient-cleanup pre-processor.

Cases are drawn from real scraped lines that Mealie's NLP parser skipped.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mealie.ingredient_cleanup import (  # noqa: E402
    ALTERNATIVE,
    COMPOUND,
    DISTRIBUTIVE,
    NORMAL,
    PARENTHETICAL,
    RECIPE_REFERENCE,
    SECTION_HEADER,
    clean_line,
)


def _texts(cl):
    return [c.text for c in cl.candidates]


# --- section headers ----------------------------------------------------------


def test_bare_section_header_for_the():
    cl = clean_line("For the pineapple chimichurri:")
    assert cl.category == SECTION_HEADER
    assert cl.auto_safe is True
    assert cl.candidates == []


def test_section_header_colon_no_for():
    cl = clean_line("Toppings:")
    assert cl.category == SECTION_HEADER


def test_labeled_ingredient_strips_prefix():
    # "For the glaze: 3 tbsp butter" is a real ingredient, not a bare header.
    cl = clean_line("For the garlic-herb butter glaze: 3 tbsp unsalted butter, melted")
    assert cl.category == NORMAL
    assert cl.auto_safe is True
    assert _texts(cl) == ["3 tbsp unsalted butter, melted"]


# --- recipe references --------------------------------------------------------


def test_recipe_reference_held():
    cl = clean_line(
        "12 slices Keto Brioche Bread Recipe (1.2 loaves; make 2 loaves)"
    )
    assert cl.category == RECIPE_REFERENCE
    assert cl.auto_safe is False
    assert cl.candidates == []


def test_recipe_reference_by_known_title():
    cl = clean_line(
        "2 cups leftover marinara base", known_titles={"leftover marinara base"}
    )
    assert cl.category == RECIPE_REFERENCE


# --- distributive "each" ------------------------------------------------------


def test_distributive_three_foods():
    cl = clean_line("1 tsp each salt, pepper, Italian seasoning, and onion powder")
    assert cl.category == DISTRIBUTIVE
    assert cl.auto_safe is True
    assert _texts(cl) == [
        "1 tsp salt",
        "1 tsp pepper",
        "1 tsp Italian seasoning",
        "1 tsp onion powder",
    ]


def test_distributive_two_foods_and_joiner():
    cl = clean_line("2 tsp each garlic powder and smoked paprika")
    assert _texts(cl) == ["2 tsp garlic powder", "2 tsp smoked paprika"]


def test_each_without_quantity_is_not_distributive():
    # "each clove" has no leading amount -> not a distribution
    cl = clean_line("each clove of garlic, minced")
    assert cl.category != DISTRIBUTIVE


# --- seasoning compounds ------------------------------------------------------


def test_salt_and_pepper_to_taste():
    cl = clean_line("Salt and pepper, to taste")
    assert cl.category == COMPOUND
    assert cl.auto_safe is True
    assert _texts(cl) == ["Salt", "pepper"]
    assert all(c.note_extra == "to taste" for c in cl.candidates)


def test_salt_and_black_pepper():
    cl = clean_line("Sea salt and black pepper, to taste")
    assert _texts(cl) == ["Sea salt", "black pepper"]


def test_no_comma_to_taste():
    cl = clean_line("salt and pepper to taste")
    assert cl.category == COMPOUND
    assert _texts(cl) == ["salt", "pepper"]


def test_non_seasoning_and_is_not_compound():
    # Two real foods joined by "and" must NOT be split as a seasoning compound.
    cl = clean_line("chicken and broccoli")
    assert cl.category != COMPOUND


# --- alternatives -------------------------------------------------------------


def test_inline_alternative_first_food_kept_held():
    cl = clean_line("2 1/2 pounds sirloin steak or shaved beef")
    assert cl.category == ALTERNATIVE
    assert cl.auto_safe is False  # which one to keep is a human call
    assert _texts(cl) == ["2 1/2 pounds sirloin steak"]
    assert cl.candidates[0].note_extra == "or shaved beef"


def test_parenthetical_alternative_is_auto_safe():
    # "(or olive oil)" is an unambiguous lift -> safe to auto-apply.
    cl = clean_line("1/4 cup avocado oil (or olive oil)")
    assert cl.category == PARENTHETICAL
    assert cl.auto_safe is True
    assert _texts(cl) == ["1/4 cup avocado oil"]
    assert cl.candidates[0].note_extra == "or olive oil"


def test_alternative_preserves_trailing_prep_note():
    cl = clean_line("1 tsp butter or olive oil, for toasting")
    assert cl.category == ALTERNATIVE
    assert _texts(cl) == ["1 tsp butter"]
    assert "or olive oil" in cl.candidates[0].note_extra


# --- parenthetical / measure noise -------------------------------------------


def test_strip_weight_parenthetical():
    cl = clean_line("1 ripe avocado, diced (about 150 g flesh)")
    assert cl.auto_safe is True
    assert _texts(cl) == ["1 ripe avocado, diced"]


def test_strip_multiple_conversion_parentheticals():
    cl = clean_line(
        "36 fl oz (1,065 g) near-boiling water, about 200°F/93°C (18 fl oz per batch)"
    )
    assert cl.auto_safe is True
    assert _texts(cl) == ["36 fl oz near-boiling water"]


def test_for_serving_lifted_to_note():
    cl = clean_line("Sliced scallions, for garnish")
    assert _texts(cl) == ["Sliced scallions"]
    assert "garnish" in cl.candidates[0].note_extra


# --- ordinary ----------------------------------------------------------------


def test_ordinary_line_passes_through():
    cl = clean_line("2 lb boneless chicken thighs")
    assert cl.category == NORMAL
    assert cl.auto_safe is True
    assert _texts(cl) == ["2 lb boneless chicken thighs"]


def test_empty_line():
    cl = clean_line("   ")
    assert cl.candidates == []
