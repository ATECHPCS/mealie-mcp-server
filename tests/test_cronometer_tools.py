"""Tests for the Mealie -> Cronometer logging tool.

The core logic is exercised directly with a fake Cronometer bridge (no
network, no env), plus one end-to-end call through the registered MCP tool
with the bridge client factory monkeypatched.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools import cronometer_tools as ct  # noqa: E402
from tools.cronometer_tools import extract_macros, log_recipe_core  # noqa: E402


class FakeCron:
    """Records add_custom_food / add_food_entry; optional pre-existing food."""

    def __init__(self, existing=None):
        self.existing = existing  # dict returned by find_custom_food, or None
        self.created = []
        self.entries = []
        self._next_food = 5000
        self._next_measure = 6000
        self._next_entry = 7000

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def find_custom_food(self, name):
        return dict(self.existing) if self.existing else None

    def add_custom_food(self, name, macros, serving_grams):
        self._next_food += 1
        self._next_measure += 1
        self.created.append(
            {"name": name, "macros": macros, "serving_grams": serving_grams}
        )
        return {"food_id": self._next_food, "measure_id": self._next_measure}

    def add_food_entry(self, food_id, measure_id, grams, date=None, diary_group=None):
        self._next_entry += 1
        self.entries.append(
            {
                "food_id": food_id,
                "measure_id": measure_id,
                "grams": grams,
                "date": date,
                "diary_group": diary_group,
            }
        )
        return str(self._next_entry)


class FakeMealie:
    """Minimal recipe source: get_recipe by slug, get_recipes by search."""

    def __init__(self, recipe=None):
        self._recipe = recipe

    def get_recipe(self, slug):
        if self._recipe and slug in (self._recipe.get("slug"), self._recipe.get("name")):
            return dict(self._recipe)
        raise Exception("404 not found")

    def get_recipes(self, search=None, per_page=None):
        if self._recipe and search and search.lower() in self._recipe["name"].lower():
            return {"items": [{"name": self._recipe["name"], "slug": self._recipe["slug"]}]}
        return {"items": []}


NUTRITIOUS = {
    "name": "Chili",
    "slug": "chili",
    "recipeServings": 4,
    "nutrition": {
        "calories": "634",
        "proteinContent": "31",
        "fatContent": "54 g",  # units are tolerated
        "carbohydrateContent": "8",
        "fiberContent": "3",
        "sodiumContent": "540",
    },
}


def test_extract_macros_parses_units_and_requires_core():
    m = extract_macros(NUTRITIOUS["nutrition"])
    assert m["calories"] == 634.0
    assert m["fat_g"] == 54.0  # "54 g" -> 54.0
    assert m["carbs_g"] == 8.0
    assert m["fiber_g"] == 3.0

    assert extract_macros({"calories": "100"}) is None  # missing protein/fat/carbs
    assert extract_macros({}) is None


def test_log_creates_food_and_entry_by_slug():
    cron = FakeCron()
    out = log_recipe_core(
        FakeMealie(NUTRITIOUS), cron, "chili", servings=2, date="2026-08-19", meal_group="dinner"
    )
    assert out["ok"] is True
    assert out["custom_food"] == "created"
    assert out["grams_logged"] == 200.0  # 2 servings * 100 g
    assert out["calories_logged"] == 1268.0  # 634 * 2
    assert out["meal_group"] == "dinner"
    assert len(cron.created) == 1
    assert cron.entries[0]["grams"] == 200.0
    assert cron.entries[0]["date"] == "2026-08-19"
    assert cron.entries[0]["diary_group"] == "dinner"


def test_log_reuses_existing_food():
    cron = FakeCron(existing={"food_id": 111, "measure_id": 222})
    out = log_recipe_core(FakeMealie(NUTRITIOUS), cron, "chili", servings=1)
    assert out["custom_food"] == "reused"
    assert out["food_id"] == 111
    assert cron.created == []  # no duplicate food
    assert cron.entries[0]["food_id"] == 111


def test_log_resolves_by_name_when_slug_lookup_fails():
    cron = FakeCron()
    out = log_recipe_core(FakeMealie(NUTRITIOUS), cron, "Chili", servings=1)
    assert out["ok"] is True
    assert out["slug"] == "chili"


def test_missing_nutrition_returns_needs_nutrition():
    no_nut = {"name": "Plain", "slug": "plain", "nutrition": {}}
    cron = FakeCron()
    out = log_recipe_core(FakeMealie(no_nut), cron, "plain")
    assert out.get("needs_nutrition") is True
    assert "error" in out
    assert cron.created == [] and cron.entries == []


def test_recipe_not_found_returns_error():
    out = log_recipe_core(FakeMealie(None), FakeCron(), "ghost")
    assert "error" in out and "No Mealie recipe" in out["error"]


def test_bad_servings_rejected():
    out = log_recipe_core(FakeMealie(NUTRITIOUS), FakeCron(), "chili", servings=0)
    assert "error" in out


@pytest.mark.asyncio
async def test_tool_registered_and_invocable(monkeypatch):
    """End-to-end through the MCP tool, with the bridge client faked."""
    from mcp.server.fastmcp import FastMCP

    fake = FakeCron()
    monkeypatch.setattr(ct, "_build_client", lambda: fake)

    mcp = FastMCP("test")
    ct.register_cronometer_tools(mcp, FakeMealie(NUTRITIOUS))

    tools = {t.name for t in await mcp.list_tools()}
    assert "log_recipe_to_cronometer" in tools

    result = await mcp.call_tool(
        "log_recipe_to_cronometer",
        {"recipe": "chili", "servings": 1, "meal_group": "lunch"},
    )
    content, structured = result if isinstance(result, tuple) else (result, None)
    payload = structured.get("result", structured) if isinstance(structured, dict) else structured
    assert payload["ok"] is True
    assert fake.entries[0]["diary_group"] == "lunch"
