"""Tests for the Mealie -> Cronometer logging tool and its bridge client.

Core logic is exercised with a fake Cronometer bridge and a fake Mealie client
(no network, no env). The bridge client's response parsing and failure handling
are tested directly against Cronometer with a fake HTTP response / call().
"""

import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cronometer import Cronometer, CronometerError  # noqa: E402
from tools import cronometer_tools as ct  # noqa: E402
from tools.cronometer_tools import (  # noqa: E402
    RecipeAmbiguousError,
    _macro_fingerprint,
    _number,
    extract_macros,
    log_recipe_core,
    resolve_recipe,
)


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
        self.created.append({"name": name, "macros": macros, "serving_grams": serving_grams})
        return {"food_id": self._next_food, "measure_id": self._next_measure}

    def add_food_entry(self, food_id, measure_id, grams, date=None, diary_group=None):
        self._next_entry += 1
        self.entries.append(
            {"food_id": food_id, "measure_id": measure_id, "grams": grams,
             "date": date, "diary_group": diary_group}
        )
        return str(self._next_entry)


class FakeMealie:
    """Recipe source keyed by slug; get_recipes returns injected search items."""

    def __init__(self, recipe=None, search_items=None):
        self._by_slug = {}
        if recipe:
            self._by_slug[recipe["slug"]] = recipe
        self._search_items = search_items  # override list for get_recipes

    def add(self, recipe):
        self._by_slug[recipe["slug"]] = recipe
        return self

    def get_recipe(self, slug):
        if slug in self._by_slug:
            return dict(self._by_slug[slug])
        raise Exception("404 not found")  # a name (non-slug) lands here -> caller searches

    def get_recipes(self, search=None, per_page=None):
        if self._search_items is not None:
            return {"items": list(self._search_items)}
        items = [
            {"name": r["name"], "slug": r["slug"]}
            for r in self._by_slug.values()
            if search and search.lower() in r["name"].lower()
        ]
        return {"items": items}


NUTRITIOUS = {
    "name": "Chili",
    "slug": "chili",
    "recipeServings": 4,
    "nutrition": {
        "calories": "634",
        "proteinContent": "31",
        "fatContent": "54 g",  # units tolerated
        "carbohydrateContent": "8",
        "fiberContent": "3",
        "sodiumContent": "540",
    },
}


# ---------------------------------------------------------------- _number

@pytest.mark.parametrize("raw,expected", [
    ("634", 634.0),
    ("54 g", 54.0),
    ("1,234", 1234.0),
    ("1,234.5 kcal", 1234.5),
    (".5", 0.5),
    ("1e3", 1000.0),
    (0, 0.0),
    (42, 42.0),
    ("0", 0.0),
    ("-5", None),      # negative macro is invalid -> rejected
    ("-5 g", None),
    ("", None),
    ("n/a", None),
    (None, None),
    (True, None),      # bool is not a nutrition value
    (float("nan"), None),
    (float("inf"), None),
])
def test_number_parsing(raw, expected):
    assert _number(raw) == expected


def test_extract_macros_requires_core_and_keeps_zero():
    m = extract_macros(NUTRITIOUS["nutrition"])
    assert m["calories"] == 634.0 and m["fat_g"] == 54.0 and m["fiber_g"] == 3.0
    # a real zero required macro is kept (presence check, not truthiness)
    z = extract_macros({"calories": "0", "proteinContent": "0", "fatContent": "0",
                        "carbohydrateContent": "0"})
    assert z == {"calories": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0}
    assert extract_macros({"calories": "100"}) is None  # missing required
    assert extract_macros({}) is None


# ---------------------------------------------------------------- resolve

def test_resolve_by_slug_direct():
    assert resolve_recipe(FakeMealie(NUTRITIOUS), "chili")["slug"] == "chili"


def test_resolve_by_exact_name_uses_search():
    # get_recipe("Chili") 404s (name != slug), so this must go through search
    assert resolve_recipe(FakeMealie(NUTRITIOUS), "Chili")["slug"] == "chili"


def test_resolve_non_exact_name_returns_none_not_fuzzy():
    # search returns only a similarly-named recipe; must NOT log the wrong one
    fm = FakeMealie(search_items=[{"name": "White Chicken Chili", "slug": "white-chicken-chili"}])
    assert resolve_recipe(fm, "chili") is None


def test_resolve_ambiguous_name_raises():
    fm = FakeMealie(search_items=[
        {"name": "Chili", "slug": "chili-1"},
        {"name": "Chili", "slug": "chili-2"},
    ])
    with pytest.raises(RecipeAmbiguousError):
        resolve_recipe(fm, "chili")


# ------------------------------------------------------------- log core

def test_log_creates_food_and_entry_by_slug():
    cron = FakeCron()
    out = log_recipe_core(FakeMealie(NUTRITIOUS), cron, "chili", servings=2,
                          date="2026-08-19", meal_group="Dinner")
    assert out["ok"] is True and out["custom_food"] == "created"
    assert out["grams_logged"] == 200.0 and out["calories_logged"] == 1268.0
    assert out["meal_group"] == "dinner"  # normalized
    assert cron.entries[0]["grams"] == 200.0
    assert cron.entries[0]["diary_group"] == "dinner"
    # the food name carries the macro fingerprint
    assert cron.created[0]["name"] == f"Chili [{_macro_fingerprint(out['macros_per_serving'])}]"


def test_log_reuses_existing_food():
    cron = FakeCron(existing={"food_id": 111, "measure_id": 222})
    out = log_recipe_core(FakeMealie(NUTRITIOUS), cron, "chili", servings=1)
    assert out["custom_food"] == "reused" and out["food_id"] == 111
    assert cron.created == []


def test_edited_macros_make_a_new_food_not_stale_reuse():
    base = _macro_fingerprint(extract_macros(NUTRITIOUS["nutrition"]))
    edited = dict(NUTRITIOUS, nutrition=dict(NUTRITIOUS["nutrition"], calories="700"))
    assert _macro_fingerprint(extract_macros(edited["nutrition"])) != base


def test_missing_nutrition_returns_needs_nutrition():
    out = log_recipe_core(FakeMealie({"name": "Plain", "slug": "plain", "nutrition": {}}),
                          FakeCron(), "plain")
    assert out.get("needs_nutrition") is True and "error" in out


def test_recipe_not_found_and_ambiguous():
    assert "No Mealie recipe" in log_recipe_core(FakeMealie(None), FakeCron(), "ghost")["error"]
    fm = FakeMealie(search_items=[{"name": "Chili", "slug": "a"}, {"name": "Chili", "slug": "b"}])
    amb = log_recipe_core(fm, FakeCron(), "chili")
    assert amb.get("ambiguous") is True


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), 1e-9])
def test_bad_servings_rejected_before_any_write(bad):
    cron = FakeCron()
    out = log_recipe_core(FakeMealie(NUTRITIOUS), cron, "chili", servings=bad)
    assert "error" in out and cron.entries == [] and cron.created == []


def test_invalid_meal_group_rejected():
    cron = FakeCron()
    out = log_recipe_core(FakeMealie(NUTRITIOUS), cron, "chili", meal_group="brunch")
    assert "error" in out and cron.entries == []


# -------------------------------------------------- bridge client (Cronometer)

def _resp(text, status=200):
    return types.SimpleNamespace(text=text, status_code=status)


def test_payload_parses_sse_and_plain_and_pretty():
    body = {"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}}
    assert Cronometer._payload(_resp(f"data: {json.dumps(body)}")) == body        # SSE + space
    assert Cronometer._payload(_resp(f"data:{json.dumps(body)}")) == body         # SSE no space
    assert Cronometer._payload(_resp(json.dumps(body))) == body                   # plain line
    assert Cronometer._payload(_resp(json.dumps(body, indent=2))) == body         # pretty body


def test_payload_malformed_raises_without_body():
    with pytest.raises(CronometerError) as e:
        Cronometer._payload(_resp("not json at all"))
    assert "not json" not in str(e.value)  # body not echoed


def _client_with_post(monkeypatch, response):
    c = Cronometer("http://bridge/mcp", "")
    c._started = True  # skip the network handshake
    monkeypatch.setattr(c, "_post", lambda body: response)
    return c


def test_call_rejects_iserror_result(monkeypatch):
    body = {"jsonrpc": "2.0", "id": 1,
            "result": {"isError": True, "content": [{"type": "text", "text": "boom"}]}}
    c = _client_with_post(monkeypatch, _resp(json.dumps(body)))
    with pytest.raises(CronometerError):
        c.call("add_food_entry", {})


def test_call_unwraps_nested_success(monkeypatch):
    inner = json.dumps({"status": "success", "entry": {"id": 42}})
    body = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": inner}]}}
    c = _client_with_post(monkeypatch, _resp(json.dumps(body)))
    assert c.call("add_food_entry", {})["entry"]["id"] == 42


def test_add_food_entry_raises_without_entry_id(monkeypatch):
    inner = json.dumps({"status": "success", "entry": {}})  # success but no id
    body = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": inner}]}}
    c = _client_with_post(monkeypatch, _resp(json.dumps(body)))
    with pytest.raises(CronometerError):
        c.add_food_entry(1, 2, 100)


def test_find_custom_food_propagates_bridge_error(monkeypatch):
    c = Cronometer("http://bridge/mcp", "")
    def boom(*a, **k):
        raise CronometerError("search_foods: timeout")
    monkeypatch.setattr(c, "call", boom)
    with pytest.raises(CronometerError):
        c.find_custom_food("Chili")  # must NOT be swallowed into None


# ---------------------------------------------------------- registration

@pytest.mark.asyncio
async def test_tool_registered_and_invocable(monkeypatch):
    from mcp.server.fastmcp import FastMCP

    fake = FakeCron()
    monkeypatch.setattr(ct, "_build_client", lambda: fake)

    mcp = FastMCP("test")
    ct.register_cronometer_tools(mcp, FakeMealie(NUTRITIOUS))
    assert "log_recipe_to_cronometer" in {t.name for t in await mcp.list_tools()}

    result = await mcp.call_tool(
        "log_recipe_to_cronometer",
        {"recipe": "chili", "servings": 1, "meal_group": "lunch"},
    )
    _, structured = result if isinstance(result, tuple) else (result, None)
    payload = structured.get("result", structured) if isinstance(structured, dict) else structured
    assert payload["ok"] is True and fake.entries[0]["diary_group"] == "lunch"


@pytest.mark.asyncio
async def test_tool_maps_bridge_error_to_error_dict(monkeypatch):
    from mcp.server.fastmcp import FastMCP

    class Boom(FakeCron):
        def add_custom_food(self, *a, **k):
            raise CronometerError("bridge down")

    monkeypatch.setattr(ct, "_build_client", lambda: Boom())
    mcp = FastMCP("test")
    ct.register_cronometer_tools(mcp, FakeMealie(NUTRITIOUS))
    result = await mcp.call_tool("log_recipe_to_cronometer", {"recipe": "chili"})
    _, structured = result if isinstance(result, tuple) else (result, None)
    payload = structured.get("result", structured) if isinstance(structured, dict) else structured
    assert "error" in payload and "ok" not in payload
