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

import hashlib
import logging
import math
import os
import re
import threading
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from cronometer import Cronometer, CronometerError
from mealie import MealieFetcher


class RecipeAmbiguousError(ValueError):
    """More than one recipe matches the given name; caller must use a slug."""


# One recipe with a given macro set maps to exactly one Cronometer custom food.
# find-then-create is not atomic across the bridge's per-call sessions, so
# serialize it in-process to avoid two concurrent logs each creating a food.
# (This does not protect multiple server replicas — there is only one here.)
_CREATE_LOCK = threading.Lock()

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

# valid Cronometer diary groups (matches the cronometer-logger skill)
_MEAL_GROUPS = frozenset({"breakfast", "lunch", "dinner", "snacks"})


# thousands-grouped ("1,234.5") first, else a plain/decimal/exponent number.
_NUM_RE = re.compile(
    r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?"
)


def _number(raw: object) -> Optional[float]:
    """Parse a Mealie nutrition value (bare or unit-suffixed string) to a float.

    Handles thousands separators ("1,234"), leading decimals (".5"), and
    exponents ("1e3"); rejects negative and non-finite values (invalid macros).
    Note: Mealie's own units are fixed per field (g for macros, mg for sodium),
    so we do NOT unit-convert — a value is taken as already in the target unit.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):  # bool is an int subclass; never a nutrition value
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
    else:
        m = _NUM_RE.search(str(raw))
        if not m:
            return None
        try:
            v = float(m.group().replace(",", ""))
        except ValueError:
            return None
    if not math.isfinite(v) or v < 0:
        return None
    return v


def _macro_fingerprint(macros: Dict[str, float]) -> str:
    """Short stable hash of the macro set.

    Baked into the custom-food name so that editing a recipe's nutrition in
    Mealie yields a NEW food instead of silently reusing the old one's macros
    (Cronometer can't update a custom food). Same recipe + same macros → same
    name → reused; changed macros → new name → new food.
    """
    parts = "|".join(
        f"{k}={round(float(macros.get(k, 0.0)), 3)}" for k in sorted(_MACRO_KEYS.values())
    )
    return hashlib.sha1(parts.encode("utf-8")).hexdigest()[:6]


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
    """Load a full recipe by slug or by EXACT (case-insensitive) name, or None.

    Never falls back to a fuzzy/first search hit — "chili" must not silently log
    "White Chicken Chili". Raises RecipeAmbiguousError when several recipes share
    the exact name (caller must pass a slug). Returns None only for a genuine
    no-match. Search list items don't carry nutrition, so a name match is
    re-fetched by slug to get the full record.
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    # exact slug hit (get_recipe is by slug; a non-slug name 404s -> fall through)
    try:
        return mealie.get_recipe(ref)
    except Exception:
        pass
    listing = mealie.get_recipes(search=ref, per_page=50)  # wide enough for exact hits
    items = (listing or {}).get("items") or []
    want = ref.lower()
    exact = [i for i in items if str(i.get("name", "")).strip().lower() == want and i.get("slug")]
    if not exact:
        return None
    if len(exact) > 1:
        raise RecipeAmbiguousError(
            f"{len(exact)} recipes are named {ref!r}; pass the exact slug instead "
            "(" + ", ".join(str(i.get("slug")) for i in exact[:5]) + ")."
        )
    return mealie.get_recipe(exact[0]["slug"])


def log_recipe_core(
    mealie: MealieFetcher,
    cron: Cronometer,
    recipe: str,
    servings: float = 1.0,
    date: Optional[str] = None,
    meal_group: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve a recipe, ensure a Cronometer food exists, and log servings.

    Returns a structured result dict, or {"error": ...} for expected input
    failures (bad servings, recipe not found, ambiguous name, no nutrition).
    Bridge failures raise CronometerError; the MCP wrapper maps those to an
    {"error": ...} response.
    """
    try:
        servings = float(servings)
    except (TypeError, ValueError):
        return {"error": "servings must be a number"}
    if not math.isfinite(servings) or servings <= 0:
        return {"error": "servings must be a positive, finite number"}

    if meal_group is not None:
        meal_group = str(meal_group).strip().lower() or None
        if meal_group and meal_group not in _MEAL_GROUPS:
            return {
                "error": f"meal_group must be one of {', '.join(sorted(_MEAL_GROUPS))} (or omitted)."
            }

    grams = round(servings * SERVING_GRAMS, 2)
    if grams <= 0:
        return {"error": "servings too small to log (rounds to 0 g)"}
    # report from the quantity actually sent, not the raw servings
    effective_servings = grams / SERVING_GRAMS

    try:
        full = resolve_recipe(mealie, recipe)
    except RecipeAmbiguousError as e:
        return {"error": str(e), "ambiguous": True}
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

    # The custom food name carries a macro fingerprint, so a recipe whose
    # nutrition changed maps to a NEW food instead of reusing stale macros
    # (Cronometer can't update a custom food). find-then-create is serialized
    # to keep two concurrent logs from both creating the same food.
    food_name = f"{name} [{_macro_fingerprint(macros)}]"
    with _CREATE_LOCK:
        food = cron.find_custom_food(food_name)
        reused = food is not None
        if not food:
            food = cron.add_custom_food(food_name, macros, SERVING_GRAMS)

    entry_id = cron.add_food_entry(
        food["food_id"], food["measure_id"], grams, date=date, diary_group=meal_group
    )

    return {
        "ok": True,
        "recipe": name,
        "slug": full.get("slug"),
        "servings": effective_servings,
        "grams_logged": grams,
        "date": date or "today",
        "meal_group": meal_group,
        "food_id": food["food_id"],
        "measure_id": food["measure_id"],
        "custom_food": "reused" if reused else "created",
        "entry_id": entry_id,
        "calories_logged": round(macros["calories"] * effective_servings, 1),
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
