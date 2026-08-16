"""Tests for the background auto-image-backfill loop: the age-window and
delay filtering that decides which recipes get an auto-generated image."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import auto_image_backfill as backfill

NOW = datetime(2026, 8, 16, 18, 0, 0, tzinfo=timezone.utc)


def _recipe(slug, minutes_ago, id_=None):
    created = NOW - timedelta(minutes=minutes_ago)
    return {"id": id_ or f"id-{slug}", "slug": slug, "createdAt": created.isoformat().replace("+00:00", "Z")}


def _mealie(items, has_image_map):
    m = MagicMock()
    m.get_recipes.return_value = {"items": items}
    m.has_image.side_effect = lambda rid: has_image_map.get(rid, False)
    return m


def test_skips_recipe_inside_grace_window():
    items = [_recipe("too-new", minutes_ago=2)]
    m = _mealie(items, {"id-too-new": False})
    with patch.object(backfill, "generate_recipe_image_core") as gen:
        acted = backfill.run_backfill_pass(m, delay_minutes=5, window_minutes=180, now=NOW)
    gen.assert_not_called()
    assert acted == []


def test_generates_after_grace_window_when_no_real_image():
    items = [_recipe("ready", minutes_ago=10)]
    m = _mealie(items, {"id-ready": False})
    with patch.object(backfill, "generate_recipe_image_core", return_value={"ok": True}) as gen:
        acted = backfill.run_backfill_pass(m, delay_minutes=5, window_minutes=180, now=NOW)
    gen.assert_called_once_with(m, "ready")
    assert acted == [{"slug": "ready", "ok": True}]


def test_skips_recipe_that_already_has_a_real_image():
    """Covers the manual-upload case: if Ian uploads his own photo inside
    the grace window, has_image() sees it and the auto-gen never fires."""
    items = [_recipe("manually-photographed", minutes_ago=10)]
    m = _mealie(items, {"id-manually-photographed": True})
    with patch.object(backfill, "generate_recipe_image_core") as gen:
        acted = backfill.run_backfill_pass(m, delay_minutes=5, window_minutes=180, now=NOW)
    gen.assert_not_called()
    assert acted == []


def test_stops_scanning_past_the_window_even_if_still_imageless():
    items = [_recipe("ancient", minutes_ago=999)]
    m = _mealie(items, {"id-ancient": False})
    with patch.object(backfill, "generate_recipe_image_core") as gen:
        acted = backfill.run_backfill_pass(m, delay_minutes=5, window_minutes=180, now=NOW)
    gen.assert_not_called()
    assert acted == []


def test_newest_first_break_does_not_skip_a_valid_recipe_before_an_old_one():
    # order_direction=desc from Mealie means newest is first; an old recipe
    # should break the scan without blocking recipes seen earlier in the list.
    items = [_recipe("ready", minutes_ago=10), _recipe("ancient", minutes_ago=999)]
    m = _mealie(items, {"id-ready": False, "id-ancient": False})
    with patch.object(backfill, "generate_recipe_image_core", return_value={"ok": True}) as gen:
        acted = backfill.run_backfill_pass(m, delay_minutes=5, window_minutes=180, now=NOW)
    gen.assert_called_once_with(m, "ready")
    assert [a["slug"] for a in acted] == ["ready"]


def test_loop_disabled_returns_without_scanning(monkeypatch):
    monkeypatch.setenv("AUTO_IMAGE_BACKFILL_ENABLED", "false")
    m = MagicMock()
    backfill.auto_image_backfill_loop(m)
    m.get_recipes.assert_not_called()


def test_loop_runs_passes_until_stopped():
    """The loop is a plain blocking while-True (runs in a daemon thread, not
    tied to the ASGI lifespan) -- verify it calls the pass repeatedly and
    respects the injected sleep function, stopping it after 3 iterations."""
    m = MagicMock()
    m.get_recipes.return_value = {"items": []}
    calls = {"n": 0}

    def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise SystemExit

    import pytest

    with pytest.raises(SystemExit):
        backfill.auto_image_backfill_loop(m, sleep=fake_sleep)
    assert m.get_recipes.call_count == 3
