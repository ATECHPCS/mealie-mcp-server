"""Tests for the recipe-authoring tools (structured ingredients, full create,
patch fields, concise output)."""


async def test_create_recipe_accepts_flat_and_structured(invoke, fetcher):
    await invoke(
        "create_recipe",
        name="Mixed",
        ingredients=[
            "200 g basmati rice",
            {
                "quantity": 2,
                "food": {"id": "f1", "name": "egg"},
                "note": "large",
                "referenceId": "a1000001-0000-4000-8000-000000000001",
            },
        ],
        instructions=[
            "Boil the rice.",
            {
                "text": "Fry the eggs.",
                "title": "Eggs",
                "ingredientReferences": [
                    {"referenceId": "a1000001-0000-4000-8000-000000000001"}
                ],
            },
        ],
    )
    body = fetcher.last("PUT", "/api/recipes/")["json"]
    ings = body["recipeIngredient"]
    steps = body["recipeInstructions"]

    # plain string -> resolved via the ingredient parser, not left as a note
    assert ings[0]["quantity"] == 1.0
    assert ings[0]["unit"]["id"] == "existing-unit"
    assert ings[0]["originalText"] == "200 g basmati rice"
    # the parser didn't recognize the food, so it was created
    food_create = fetcher.last("POST", "/api/foods")
    assert food_create["json"]["name"] == "200 g basmati rice"
    assert ings[0]["food"]["id"] == "generated-0001"
    assert ings[1]["quantity"] == 2
    assert ings[1]["food"] == {
        "id": "f1",
        "name": "egg",
        "description": "",
        "aliases": [],
        "householdsWithIngredientFood": [],
    }
    # already a valid UUID -> passes through unchanged
    assert ings[1]["referenceId"] == "a1000001-0000-4000-8000-000000000001"
    assert steps[0]["ingredientReferences"] == []
    assert steps[1]["title"] == "Eggs"
    assert steps[1]["ingredientReferences"] == [
        {"referenceId": "a1000001-0000-4000-8000-000000000001"}
    ]


async def test_import_recipe_from_url_parses_scraped_ingredients(invoke, fetcher, monkeypatch):
    # exercise the basic-parse path (the opt-out / fail-safe fallback); the
    # default advanced-cleanup-on-import path is covered in test_import_cleanup
    monkeypatch.setenv("MEALIE_IMPORT_AUTO_CLEANUP", "false")
    fetcher.recipe = {
        **fetcher.recipe,
        "recipeIngredient": [
            {"note": "2 lb chicken thighs", "food": None, "referenceId": "ref-1"},
            {"note": "", "food": None, "referenceId": "ref-2"},
        ],
    }

    await invoke("import_recipe_from_url", url="https://example.com/r")

    # only the non-blank, unresolved note gets sent to the parser
    parse_call = fetcher.last("POST", "/api/parser/ingredients")
    assert parse_call["json"]["ingredients"] == ["2 lb chicken thighs"]

    patch = fetcher.last("PATCH", "/api/recipes/")["json"]
    ings = patch["recipeIngredient"]
    # the ingredient's own referenceId is preserved so instruction links survive
    assert ings[0]["referenceId"] == "ref-1"
    assert ings[0]["originalText"] == "2 lb chicken thighs"
    assert ings[0]["food"]["id"] == "generated-0001"
    # the blank entry was left untouched, not sent to the parser
    assert ings[1] == {"note": "", "food": None, "referenceId": "ref-2"}


async def test_create_recipe_full_sets_metadata_tags_tools_and_image(invoke, fetcher):
    await invoke(
        "create_recipe_full",
        name="Full",
        description="A dish",
        org_url="https://example.com/r",
        total_time="30 min",
        prep_time="10 min",
        recipe_yield="4 Portionen",
        servings=2,
        image_url="https://example.com/img.jpg",
        ingredients=["1 onion"],
        instructions=["Chop the onion."],
        tags=[{"id": "t1", "name": "Quick"}],
        tools=[{"id": "k1", "name": "Pfanne"}],
    )
    body = fetcher.last("PUT", "/api/recipes/")["json"]
    assert body["description"] == "A dish"
    assert body["orgURL"] == "https://example.com/r"
    assert body["totalTime"] == "30 min"
    assert body["recipeServings"] == 2
    assert body["recipeYield"] == "4 Portionen"
    assert body["recipeIngredient"][0]["originalText"] == "1 onion"
    assert body["recipeIngredient"][0]["food"]["id"] == "generated-0001"
    # slug derived from the name (Mealie requires it on organizer refs)
    assert body["tags"] == [{"id": "t1", "name": "Quick", "slug": "quick"}]
    assert body["tools"][0]["id"] == "k1"
    assert body["tools"][0]["slug"] == "pfanne"
    # image is scraped server-side after the recipe content is written
    assert fetcher.last("POST", "/image") is not None


async def test_patch_recipe_maps_all_fields(invoke, fetcher):
    await invoke(
        "patch_recipe",
        slug="test-recipe",
        total_time="35 min",
        prep_time="10 min",
        cook_time="25 min",
        perform_time="20 min",
        servings=4,
        org_url="https://example.com/r",
        recipe_yield="4 Portionen",
        tags=[{"id": "t1", "name": "Quick"}],
        tools=[{"id": "k1", "name": "Pfanne"}],
    )
    body = fetcher.last("PATCH", "/api/recipes/")["json"]
    assert body == {
        "recipeYield": "4 Portionen",
        "recipeServings": 4,
        "totalTime": "35 min",
        "prepTime": "10 min",
        "cookTime": "25 min",
        "performTime": "20 min",
        "orgURL": "https://example.com/r",
        "tags": [{"id": "t1", "name": "Quick", "slug": "quick"}],
        "tools": [{"id": "k1", "name": "Pfanne", "slug": "pfanne"}],
    }


async def test_get_recipe_concise_includes_orgurl_tags_tools(invoke, fetcher):
    fetcher.recipe = {
        **fetcher.recipe,
        "orgURL": "https://example.com/r",
        "tags": [{"id": "t1", "name": "Quick", "slug": "quick"}],
        "tools": [
            {"id": "k1", "name": "Pfanne", "slug": "pfanne", "householdsWithTool": []}
        ],
    }
    out = await invoke("get_recipe_concise", slug="test-recipe")
    assert out["orgURL"] == "https://example.com/r"
    assert out["tags"] == [{"id": "t1", "name": "Quick", "slug": "quick"}]
    assert out["tools"][0]["name"] == "Pfanne"


# --- set_recipe_image_from_url resiliency ------------------------------------
# Mealie's scrape endpoint 200s even when it stored no image, so the tool must
# verify the real media file (has_image) and return an honest, per-call
# result instead of a bland success that invites a blind retry loop.

async def test_set_recipe_image_from_url_ok_when_image_lands(invoke, fetcher):
    fetcher.has_image = lambda recipe_id: True

    out = await invoke(
        "set_recipe_image_from_url",
        slug="test-recipe",
        image_url="https://example.com/pic.jpg",
    )

    assert out["ok"] is True
    assert out["image_url"] == "https://example.com/pic.jpg"
    # it actually asked Mealie to scrape the URL
    scrape = fetcher.last("POST", "/api/recipes/test-recipe/image")
    assert scrape["json"] == {"url": "https://example.com/pic.jpg"}


async def test_set_recipe_image_from_url_reports_failure_when_no_image_stored(
    invoke, fetcher
):
    # default FakeFetcher.has_image resolves False (no _client media file)
    out = await invoke(
        "set_recipe_image_from_url",
        slug="test-recipe",
        image_url="https://example.com/not-an-image",
    )

    assert out["ok"] is False
    assert out["image_url"] == "https://example.com/not-an-image"
    # steers the caller off the retry loop and toward generation
    assert "generate_recipe_image" in out["message"]


async def test_set_recipe_image_from_url_swallows_scrape_error(invoke, fetcher):
    def boom(slug, image_url):
        raise RuntimeError("422 Unprocessable Entity")

    fetcher.scrape_recipe_image_from_url = boom

    # must NOT raise a ToolError; returns a structured ok=False instead
    out = await invoke(
        "set_recipe_image_from_url",
        slug="test-recipe",
        image_url="https://example.com/blocked.jpg",
    )

    assert out["ok"] is False
    assert "422" in out["message"]
    assert "generate_recipe_image" in out["message"]


async def test_create_recipe_full_survives_bad_image_url(invoke, fetcher):
    def boom(slug, image_url):
        raise RuntimeError("could not fetch image")

    fetcher.scrape_recipe_image_from_url = boom

    # the recipe is created even though the image scrape blows up
    out = await invoke(
        "create_recipe_full",
        name="Imageless",
        image_url="https://example.com/dead.jpg",
    )

    assert isinstance(out, dict)
    # the create/update happened despite the image failure
    assert fetcher.last("PUT", "/api/recipes/") is not None


async def test_import_recipe_with_ai_posts_multipart_and_returns_recipe(invoke, fetcher, monkeypatch):
    monkeypatch.setenv("MEALIE_IMPORT_AUTO_CLEANUP", "false")
    await invoke(
        "import_recipe_with_ai",
        url="https://www.tiktok.com/@chef/video/1",
        content="extra notes",
    )
    call = fetcher.last("POST", "/api/recipes/create/ai")
    assert call["files"]["url"] == (None, "https://www.tiktok.com/@chef/video/1")
    assert call["files"]["content"] == (None, "extra notes")
    assert call["files"]["createNewOrganizers"] == (None, "false")
    assert "translateLanguage" not in call["files"]
    assert call["timeout"] == 600.0
    assert fetcher.last("GET", "/api/recipes/")  # created recipe fetched back


async def test_import_recipe_with_ai_requires_url_or_content(invoke, fetcher):
    import pytest
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        await invoke("import_recipe_with_ai", content="   ")
    assert not any(r["url"] == "/api/recipes/create/ai" for r in fetcher.requests)


async def test_import_with_ai_stops_before_ai_on_known_url(invoke, fetcher):
    fetcher.existing_recipes = [
        {"slug": "old", "name": "Old Shrimp", "orgURL": "https://www.example.com/shrimp"}
    ]
    res = await invoke("import_recipe_with_ai", url="https://example.com/shrimp/")
    assert res["duplicate"] is True and res["needs_confirmation"] is True
    assert res["existing"][0]["slug"] == "old"
    assert not any(r["url"] == "/api/recipes/create/ai" for r in fetcher.requests)


async def test_import_with_ai_deletes_new_copy_on_same_name(invoke, fetcher, monkeypatch):
    monkeypatch.setenv("MEALIE_IMPORT_AUTO_CLEANUP", "false")
    ings = [{"food": {"name": f}, "note": ""} for f in ("egg", "tortilla", "cheese", "salsa")]
    fetcher.recipe = {**fetcher.recipe, "recipeIngredient": ings}
    fetcher.existing_recipes = [{"slug": "test-recipe-old", "name": "Test Recipe", "orgURL": None}]
    res = await invoke("import_recipe_with_ai", content="some recipe text")
    assert res["duplicate"] is True
    assert res["removed_new_copy"] == "Test Recipe"
    assert fetcher.last("DELETE", "/api/recipes/test-recipe")


async def test_uncomparable_same_name_is_kept_with_a_warning(invoke, fetcher, monkeypatch):
    monkeypatch.setenv("MEALIE_IMPORT_AUTO_CLEANUP", "false")
    fetcher.existing_recipes = [{"slug": "test-recipe-old", "name": "Test Recipe", "orgURL": None}]
    res = await invoke("import_recipe_with_ai", content="some recipe text")  # no ingredients
    assert not res.get("duplicate")
    assert res["possible_duplicates"][0]["slug"] == "test-recipe-old"
    assert not fetcher.last("DELETE", "/api/recipes/")


async def test_import_from_url_allow_duplicate_skips_check(invoke, fetcher, monkeypatch):
    monkeypatch.setenv("MEALIE_IMPORT_AUTO_CLEANUP", "false")
    fetcher.existing_recipes = [{"slug": "test-recipe-old", "name": "Test Recipe", "orgURL": None}]
    res = await invoke("import_recipe_from_url", url="https://example.com/r", allow_duplicate=True)
    assert not res.get("duplicate")
    assert not fetcher.last("DELETE", "/api/recipes/")
    # no library fetch when duplicates are allowed
    assert not any(r["method"] == "GET" and r["url"] == "/api/recipes" for r in fetcher.requests)


async def test_same_name_with_different_ingredients_is_not_a_duplicate(invoke, fetcher, monkeypatch):
    monkeypatch.setenv("MEALIE_IMPORT_AUTO_CLEANUP", "false")
    ing = lambda *fs: [{"food": {"name": f}, "note": ""} for f in fs]  # noqa: E731
    fetcher.recipe = {**fetcher.recipe, "recipeIngredient": ing("egg", "tortilla", "cheese", "salsa", "onion")}
    fetcher.existing_recipes = [{"slug": "other-migas", "name": "Test Recipe", "orgURL": None}]
    fetcher.recipes_by_slug = {"other-migas": {**fetcher.recipe, "slug": "other-migas",
                                               "recipeIngredient": ing("egg", "chips", "jack", "pico", "avocado")}}
    res = await invoke("import_recipe_with_ai", content="migas")
    assert not res.get("duplicate")
    assert not fetcher.last("DELETE", "/api/recipes/")
