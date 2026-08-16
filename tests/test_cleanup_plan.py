"""Tests for the cleanup orchestrator's pure planning/apply logic.

The NLP parser is faked so these run without a live Mealie: the fake mirrors
Mealie's behaviour of returning quantity/unit/food + an average confidence.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mealie.cleanup import (  # noqa: E402
    AUTO,
    RECIPE_REF,
    REVIEW,
    SECTION,
    apply_plan,
    build_plan,
)


def _parsed(input_text, food=None, food_id=None, qty=1, unit=None, conf=0.99):
    return {
        "input": input_text,
        "confidence": {"average": conf},
        "ingredient": {
            "quantity": qty,
            "unit": {"name": unit} if unit else None,
            "food": {"name": food, "id": food_id} if food else None,
            "note": "",
        },
    }


def make_parser(table):
    """table: dict of candidate-text -> parsed dict (or a default high-conf)."""

    def parse(texts):
        out = []
        for t in texts:
            if t in table:
                out.append(table[t])
            else:
                # default: confidently matches a food named after the last word
                out.append(_parsed(t, food=t.split()[-1], qty=1, conf=0.97))
        return out

    return parse


def _ing(note):
    return {"note": note, "food": None, "referenceId": "r-" + note[:4]}


# --- planning -----------------------------------------------------------------


def test_compound_line_becomes_auto_two_entries():
    raw = [_ing("Salt and pepper, to taste")]
    table = {
        "Salt": _parsed("Salt", food="salt", qty=0, conf=0.95),
        "pepper": _parsed("pepper", food="pepper", qty=0, conf=0.95),
    }
    plan = build_plan(raw, make_parser(table), food_names=["salt", "pepper"])
    line = plan["lines"][0]
    assert line["disposition"] == AUTO
    assert [p["food"] for p in line["proposals"]] == ["salt", "pepper"]
    assert plan["counts"][AUTO] == 1


def test_alternative_line_is_held_for_review():
    raw = [_ing("2 1/2 pounds sirloin steak or shaved beef")]
    plan = build_plan(raw, make_parser({}), food_names=[])
    line = plan["lines"][0]
    assert line["disposition"] == REVIEW  # auto_safe False even if confident
    assert line["proposals"][0]["food"]  # a best-guess primary is still offered
    assert "or shaved beef" in line["proposals"][0]["note"]


def test_low_confidence_line_is_reviewed():
    raw = [_ing("a pinch of something weird")]
    table = {"a pinch of something weird": _parsed("x", food="something", conf=0.40)}
    plan = build_plan(raw, make_parser(table), food_names=[])
    assert plan["lines"][0]["disposition"] == REVIEW


def test_section_header_disposition():
    raw = [_ing("For the pineapple chimichurri:")]
    plan = build_plan(raw, make_parser({}), food_names=[])
    line = plan["lines"][0]
    assert line["disposition"] == SECTION
    assert line["section_title"] == "For the pineapple chimichurri"


def test_recipe_reference_disposition():
    raw = [_ing("12 slices Keto Brioche Bread Recipe")]
    plan = build_plan(raw, make_parser({}), food_names=[])
    assert plan["lines"][0]["disposition"] == RECIPE_REF


def test_dedup_reuses_existing_food_id():
    raw = [_ing("1 cup hot water")]
    table = {"1 cup hot water": _parsed("1 cup hot water", food="hot water", qty=1, unit="cup", conf=0.95)}
    plan = build_plan(
        raw,
        make_parser(table),
        food_names=["water"],
        food_id_by_name={"water": "water-id-1"},
    )
    prop = plan["lines"][0]["proposals"][0]
    assert prop["food"] == "water"
    assert prop["food_id"] == "water-id-1"
    assert prop["food_source"] == "dedup:water"


def test_structured_line_is_ignored():
    raw = [{"note": "chicken", "food": {"id": "abc", "name": "chicken"}}]
    plan = build_plan(raw, make_parser({}), food_names=[])
    assert plan["unstructured"] == 0
    assert plan["lines"] == []


# --- apply --------------------------------------------------------------------


def test_apply_expands_compound_into_two_entries():
    raw = [_ing("Salt and pepper, to taste")]
    table = {
        "Salt": _parsed("Salt", food="salt", qty=0, conf=0.95),
        "pepper": _parsed("pepper", food="pepper", qty=0, conf=0.95),
    }
    plan = build_plan(raw, make_parser(table), food_names=["salt", "pepper"])

    created = []

    def ensure_food(name, food_id):
        obj = {"id": food_id or f"new-{name}", "name": name}
        if not food_id:
            created.append(name)
        return obj

    result = apply_plan(raw, plan, ensure_food, lambda n: None)
    assert result["applied"] == 1
    assert len(result["ingredients"]) == 2  # one line became two
    assert [e["food"]["name"] for e in result["ingredients"]] == ["salt", "pepper"]
    # first keeps the original referenceId, the inserted one does not
    assert result["ingredients"][0]["referenceId"] == "r-Salt"
    assert result["ingredients"][1]["referenceId"] is None


def test_apply_skips_review_unless_opted_in():
    raw = [_ing("2 cups baby arugula or baby greens")]
    plan = build_plan(raw, make_parser({}), food_names=[])
    # default: review lines are left untouched
    r1 = apply_plan(raw, plan, lambda n, i: {"id": "x", "name": n}, lambda n: None)
    assert r1["applied"] == 0
    assert r1["ingredients"][0] is raw[0]
    # opt in: the best-guess primary is applied
    r2 = apply_plan(
        raw, plan, lambda n, i: {"id": "x", "name": n}, lambda n: None,
        apply_reviews=True,
    )
    assert r2["applied"] == 1


def test_apply_section_sets_title_and_clears_food():
    raw = [_ing("For the sauce:")]
    plan = build_plan(raw, make_parser({}), food_names=[])
    result = apply_plan(raw, plan, lambda n, i: None, lambda n: None)
    entry = result["ingredients"][0]
    assert entry["title"] == "For the sauce"
    assert entry["food"] is None
    assert entry["quantity"] == 0
