"""Tests for auto-cleanup wired into the recipe import path."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import tools.recipe_tools as rt  # noqa: E402


class _StubMealie:
    """Records which cleanup path ran."""

    def __init__(self, cleanup_raises=False):
        self.cleanup_calls = []
        self.basic_parse = 0
        self.cleanup_raises = cleanup_raises
        self._recipe = {"slug": "r", "recipeIngredient": []}

    def apply_recipe_cleanup(self, slug, apply_reviews=False):
        self.cleanup_calls.append({"slug": slug, "apply_reviews": apply_reviews})
        if self.cleanup_raises:
            raise RuntimeError("boom")
        return {"applied": 3}

    def get_recipe(self, slug):
        return self._recipe

    # parse_ingredients/patch_recipe used by the basic-parse fallback
    def parse_ingredients(self, notes):
        self.basic_parse += 1
        return [{"input": n, "confidence": {}, "ingredient": {}} for n in notes]

    def patch_recipe(self, slug, data):
        return self._recipe


def _clear_env(monkeypatch):
    monkeypatch.delenv("MEALIE_IMPORT_AUTO_CLEANUP", raising=False)
    monkeypatch.delenv("MEALIE_IMPORT_CLEANUP_REVIEWS", raising=False)


def test_env_flag_defaults_and_parsing(monkeypatch):
    _clear_env(monkeypatch)
    assert rt._env_flag("MEALIE_IMPORT_AUTO_CLEANUP", True) is True
    assert rt._env_flag("MEALIE_IMPORT_CLEANUP_REVIEWS", False) is False
    monkeypatch.setenv("MEALIE_IMPORT_AUTO_CLEANUP", "false")
    assert rt._env_flag("MEALIE_IMPORT_AUTO_CLEANUP", True) is False
    monkeypatch.setenv("MEALIE_IMPORT_AUTO_CLEANUP", "ON")
    assert rt._env_flag("MEALIE_IMPORT_AUTO_CLEANUP", False) is True


def test_cleanup_runs_by_default_with_reviews(monkeypatch):
    _clear_env(monkeypatch)
    m = _StubMealie()
    rt._clean_ingredients_after_save(m, "r", {"recipeIngredient": []})
    assert m.cleanup_calls == [{"slug": "r", "apply_reviews": True}]
    assert m.basic_parse == 0  # cleanup subsumes the basic parse


def test_reviews_toggle_off(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MEALIE_IMPORT_CLEANUP_REVIEWS", "false")
    m = _StubMealie()
    rt._clean_ingredients_after_save(m, "r", {"recipeIngredient": []})
    assert m.cleanup_calls[0]["apply_reviews"] is False


def test_disabled_falls_back_to_basic_parse(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MEALIE_IMPORT_AUTO_CLEANUP", "false")
    m = _StubMealie()
    rt._clean_ingredients_after_save(m, "r", {"recipeIngredient": []})
    assert m.cleanup_calls == []  # cleanup skipped


def test_cleanup_error_is_fail_safe(monkeypatch):
    _clear_env(monkeypatch)
    m = _StubMealie(cleanup_raises=True)
    # must NOT raise — an import can't be broken by a cleanup hiccup
    out = rt._clean_ingredients_after_save(m, "r", {"recipeIngredient": []})
    assert out == m._recipe
    assert len(m.cleanup_calls) == 1  # attempted, then fell back
