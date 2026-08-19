"""Log a Mealie recipe's macros to the Cronometer diary in one call.

This closes the gap that made a raw agent's "import this recipe into
Cronometer" fail: there was no single tool, only the low-level bridge
primitives (add_custom_food + add_food_entry) that had to be hand-chained.

Flow (mirrors grocy-cook's eat.py, minus the Grocy stock side):

  1. resolve the recipe by slug or name and read its per-serving nutrition,
  2. reuse this recipe's Cronometer custom food, or create one (Cronometer
     cannot update a custom food, so we look for an existing one by name
     before making a duplicate),
  3. log servings x macros to the diary.

The custom food is defined per SERVING_GRAMS grams and its macros are the
per-serving macros, so Cronometer's linear-by-grams scaling means N servings
is logged as N * SERVING_GRAMS grams. SERVING_GRAMS is only a unit of account.
"""

import logging
import os
import re
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from cronometer import Cronometer, CronometerError
from mealie import MealieFetcher

logger = logging.getLogger("mealie-mcp")

# nominal weight of one serving; see module docstring.
SERVING_GRAMS = 100.0

# Mealie nutrition key -> Cronometer macro key. Mealie stores these per serving.
_MACRO_KEYS = {
    "calories": "calories",
    "proteinContent": "protein_g",
    "fatContent": "fat_g",
    "carbohydrateContent": "carbs_g",
    "fiberContent": "fiber_g",
    "sugarContent": "sugar_g",
    "sodiumContent": "sodium_mg",
    "saturatedFatContent": "saturated_fat_g",
}
# the four Cronometer's add_custom_food requires
_REQUIRED = ("calories", "protein_g", "fat_g", "carbs_g")


def _number(raw: object) -> Optional[float]:
    """Mealie stores nutrition as strings, sometimes with units ("54 g")."""
    if raw is None:
        return None
    m = re.search(r"\d+(?:\.\d+)?", str(raw))
    return float(m.group()) if m else None


def extract_macros(nutrition: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Per-serving macros from a Mealie recipe's nutrition block.

    Returns None when any of the four macros Cronometer requires is missing —
    without them there is nothing loggable.
    """
    macros: Dict[str, float] = {}
    for src, dest in _MACRO_KEYS.items():
        v = _number((nutrition or {}).get(src))
        if v is not None:
            macros[dest] = v
    if any(k not in macros for k in _REQUIRED):
        return None
    return macros


def resolve_recipe(mealie: MealieFetcher, ref: str) -> Optional[Dict[str, Any]]:
    """Load a full recipe by slug or by (case-insensitive) name, or None.

    Search list items don't carry nutrition, so a name match is re-fetched by
    slug to get the full record.
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    try:
        return mealie.get_recipe(ref)
    except Exception:
        pass
    try:
        listing = mealie.get_recipes(search=ref, per_page=5)
    except Exception:
        return None
    items = (listing or {}).get("items") or []
    if not items:
        return None
    want = ref.lower()
    match = next((i for i in items if str(i.get("name", "")).strip().lower() == want), items[0])
    slug = match.get("slug")
    if not slug:
        return None
    try:
        return mealie.get_recipe(slug)
    except Exception:
        return None


def log_recipe_core(
    mealie: MealieFetcher,
    cron: Cronometer,
    recipe: str,
    servings: float = 1.0,
    date: Optional[str] = None,
    meal_group: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve a recipe, ensure a Cronometer food exists, and log servings.

    Returns a structured result dict, or {"error": ...} on any expected
    failure (recipe not found, no nutrition, bridge error). Never raises for
    those; the caller surfaces the message.
    """
    if servings <= 0:
        return {"error": "servings must be greater than 0"}

    full = resolve_recipe(mealie, recipe)
    if not full:
        return {"error": f"No Mealie recipe found for {recipe!r}."}

    name = str(full.get("name") or recipe).strip()
    macros = extract_macros(full.get("nutrition") or {})
    if macros is None:
        return {
            "error": (
                f"Recipe {name!r} has no usable nutrition in Mealie "
                "(needs calories, protein, fat and carbs per serving). "
                "Set it with set_recipe_nutrition first, then retry."
            ),
            "recipe": name,
            "slug": full.get("slug"),
            "needs_nutrition": True,
        }

    # Reuse this recipe's custom food if one already exists (Cronometer has no
    # update/delete for custom foods, so a duplicate would accumulate forever).
    reused = True
    food = cron.find_custom_food(name)
    if not food:
        reused = False
        food = cron.add_custom_food(name, macros, SERVING_GRAMS)

    grams = round(float(servings) * SERVING_GRAMS, 2)
    entry_id = cron.add_food_entry(
        food["food_id"], food["measure_id"], grams, date=date, diary_group=meal_group
    )

    return {
        "ok": True,
        "recipe": name,
        "slug": full.get("slug"),
        "servings": float(servings),
        "grams_logged": grams,
        "date": date or "today",
        "meal_group": meal_group,
        "food_id": food["food_id"],
        "measure_id": food["measure_id"],
        "custom_food": "reused" if reused else "created",
        "entry_id": entry_id,
        "calories_logged": round(macros["calories"] * float(servings), 1),
        "macros_per_serving": macros,
    }


def _build_client() -> Cronometer:
    """Construct the bridge client from the environment.

    Kept as a seam so tests can substitute a fake without env or network.
    """
    url = os.getenv("CRONOMETER_MCP_URL", "").strip()
    token = os.getenv("CRONOMETER_MCP_TOKEN", "").strip()
    if not url:
        raise CronometerError(
            "CRONOMETER_MCP_URL is not set; the Cronometer bridge is not configured."
        )
    return Cronometer(url=url, token=token)


def register_cronometer_tools(mcp: FastMCP, mealie: MealieFetcher) -> None:
    """Register the Mealie -> Cronometer logging tool."""

    @mcp.tool()
    def log_recipe_to_cronometer(
        recipe: str,
        servings: float = 1.0,
        date: Optional[str] = None,
        meal_group: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Log a Mealie recipe's macros to the Cronometer diary.

        Reads the recipe's PER-SERVING nutrition from Mealie and logs
        `servings` of it to Cronometer, creating (or reusing) one custom food
        per recipe. Use when someone says they ate a recipe and wants it in
        Cronometer.

        The recipe MUST have nutrition set in Mealie (calories, protein, fat,
        carbs per serving); if it doesn't, this returns needs_nutrition and you
        should call set_recipe_nutrition first.

        Args:
            recipe: Recipe slug or exact name.
            servings: How many servings were eaten (default 1).
            date: Diary date YYYY-MM-DD (default: Cronometer's today).
            meal_group: Diary group — one of breakfast, lunch, dinner, snacks
                (optional; Cronometer picks a default if omitted).
        """
        try:
            cron = _build_client()
        except CronometerError as e:
            return {"error": str(e)}
        try:
            with cron:
                return log_recipe_core(mealie, cron, recipe, servings, date, meal_group)
        except CronometerError as e:
            logger.error({"message": "Cronometer log failed", "error": str(e)})
            return {"error": f"Cronometer bridge error: {e}"}
        except Exception as e:  # noqa: BLE001 - surface unexpected failures to the caller
            logger.exception("Unexpected error logging recipe to Cronometer")
            return {"error": f"Unexpected error: {e}"}
