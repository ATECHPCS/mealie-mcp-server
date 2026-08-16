"""Recipe-ingredient cleanup orchestrator.

Ties together the network-free pieces (`ingredient_cleanup`, `food_dedupe`)
with Mealie's own NLP parser and catalog APIs to turn a recipe's raw,
unstructured ingredient lines into linked quantity/unit/food entries.

Flow for one recipe:

  1. Find ingredient lines that still have free text but no linked food.
  2. Pre-process each with `clean_line` — split compounds/distributions, strip
     measure noise, lift alternatives into a note, flag headers/recipe refs.
  3. Re-parse the cleaned candidate text with Mealie's NLP parser (the same
     engine, now fed input it can actually handle).
  4. Dedupe any newly-named food against the catalog so we reuse "water"
     instead of minting "hot water".
  5. Decide a disposition per line: `auto` (deterministic + confident),
     `review` (ambiguous — which alternative to keep, low confidence),
     `section` (promote to a titled group), or `recipe_ref` (needs a manual
     sub-recipe link).

`build_plan` and `apply_plan` are pure given injected callables, so they unit
test without a live Mealie. `CleanupMixin` wires them to the real client.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from .food_dedupe import find_duplicate, norm
from .ingredient_cleanup import (
    ACTION_RECIPE_REF,
    ACTION_SECTION,
    clean_line,
)
from .ingredient_cleanup import (
    is_recipe_reference as _is_recipe_ref_name,
)

logger = logging.getLogger("mealie-mcp")

DEFAULT_CONFIDENCE = 0.85

# Dispositions
AUTO = "auto"
REVIEW = "review"
SECTION = "section"
RECIPE_REF = "recipe_ref"
SKIP = "skip"


def _line_text(ing: Dict[str, Any]) -> str:
    """Best raw text for an unstructured ingredient object."""
    return (ing.get("note") or ing.get("originalText") or ing.get("display") or "").strip()


def _is_unstructured(ing: Dict[str, Any]) -> bool:
    return bool(_line_text(ing)) and not (ing.get("food") or {}).get("id")


def _merge_note(*parts: str) -> str:
    seen: List[str] = []
    for p in parts:
        p = (p or "").strip().strip(";").strip()
        if p and p not in seen:
            seen.append(p)
    return "; ".join(seen)


def build_plan(
    raw_ings: List[Dict[str, Any]],
    parse_fn: Callable[[List[str]], List[Dict[str, Any]]],
    food_names: Optional[List[str]] = None,
    food_id_by_name: Optional[Dict[str, str]] = None,
    known_titles: Optional[set] = None,
    confidence: float = DEFAULT_CONFIDENCE,
) -> Dict[str, Any]:
    """Produce a cleanup plan for a recipe's ingredient list (no mutation).

    Args:
        raw_ings: the recipe's recipeIngredient list.
        parse_fn: callable taking a list of raw strings and returning Mealie
            ParsedIngredient dicts, in order (i.e. `mealie.parse_ingredients`).
        food_names: catalog food names, for dedupe.
        food_id_by_name: normalised-name -> food id, to resolve a dedupe hit to
            a concrete existing food id.
        known_titles: recipe titles, so sub-recipe references are detected.
        confidence: minimum NLP average confidence to accept a parse.

    Returns:
        A plan dict with per-line dispositions and proposed structured entries.
    """
    food_names = food_names or []
    food_id_by_name = food_id_by_name or {}

    # 1) classify every unstructured line, gather candidate texts to parse
    lines: List[Dict[str, Any]] = []
    to_parse: List[str] = []
    for idx, ing in enumerate(raw_ings):
        if not _is_unstructured(ing):
            continue
        raw = _line_text(ing)
        cl = clean_line(raw, known_titles=known_titles)
        span = (len(to_parse), len(to_parse) + len(cl.candidates))
        to_parse.extend(c.text for c in cl.candidates)
        lines.append({"index": idx, "cleaned": cl, "span": span})

    parsed = parse_fn(to_parse) if to_parse else []

    # 2) turn each classified line into proposals + a disposition
    out_lines: List[Dict[str, Any]] = []
    counts = {AUTO: 0, REVIEW: 0, SECTION: 0, RECIPE_REF: 0, SKIP: 0}
    for entry in lines:
        cl = entry["cleaned"]
        raw = cl.raw
        rec: Dict[str, Any] = {
            "index": entry["index"],
            "raw": raw,
            "category": cl.category,
            "auto_safe": cl.auto_safe,
            "reason": cl.reason,
            "proposals": [],
        }

        if cl.action == ACTION_SECTION:
            rec["disposition"] = SECTION
            rec["section_title"] = raw.rstrip(":").strip()
            counts[SECTION] += 1
            out_lines.append(rec)
            continue

        if cl.action == ACTION_RECIPE_REF:
            rec["disposition"] = RECIPE_REF
            counts[RECIPE_REF] += 1
            out_lines.append(rec)
            continue

        lo, hi = entry["span"]
        proposals = []
        all_ok = True
        for cand, p in zip(cl.candidates, parsed[lo:hi]):
            ing_obj = (p or {}).get("ingredient") or {}
            food = ing_obj.get("food") or {}
            unit = ing_obj.get("unit") or {}
            conf = ((p or {}).get("confidence") or {}).get("average") or 0
            food_name = food.get("name")
            food_id = food.get("id")
            food_source = "none"
            if food_name and not food_id:
                dupe = find_duplicate(food_name, food_names)
                if dupe:
                    food_id = food_id_by_name.get(norm(dupe))
                    food_name = dupe
                    food_source = f"dedup:{dupe}"
                else:
                    food_source = "new"
            elif food_id:
                food_source = "existing"

            is_recipe_ref = bool(food_name) and _is_recipe_ref_name(food_name)
            ok = conf >= confidence and bool(food_name) and not is_recipe_ref
            all_ok = all_ok and ok
            proposals.append(
                {
                    "text": cand.text,
                    "confidence": round(conf, 3),
                    "food": food_name,
                    "food_id": food_id,
                    "food_source": food_source,
                    "unit": unit.get("name"),
                    "unit_id": unit.get("id"),
                    "quantity": ing_obj.get("quantity") or 0,
                    "note": _merge_note(cand.note_extra, ing_obj.get("note") or ""),
                    "ok": ok,
                }
            )
        rec["proposals"] = proposals

        if proposals and all_ok and cl.auto_safe:
            rec["disposition"] = AUTO
            counts[AUTO] += 1
        elif proposals:
            rec["disposition"] = REVIEW
            counts[REVIEW] += 1
        else:
            rec["disposition"] = SKIP
            counts[SKIP] += 1
        out_lines.append(rec)

    return {
        "total_ingredients": len(raw_ings),
        "unstructured": len(lines),
        "counts": counts,
        "lines": out_lines,
    }


def apply_plan(
    raw_ings: List[Dict[str, Any]],
    plan: Dict[str, Any],
    ensure_food: Callable[[str, Optional[str]], Optional[Dict[str, Any]]],
    ensure_unit: Callable[[str], Optional[Dict[str, Any]]],
    apply_reviews: bool = False,
) -> Dict[str, Any]:
    """Build the new recipeIngredient list from a plan.

    Args:
        raw_ings: the original recipeIngredient list.
        plan: output of `build_plan`.
        ensure_food: (name, food_id) -> food object to link (create/reuse).
        ensure_unit: name -> unit object to link (create/reuse), or None.
        apply_reviews: also apply REVIEW lines (use their first, best guess).

    Returns:
        {"ingredients": [...new list...], "applied": n, "created": [...]}.
        Lines left for review/manual link keep their original entry untouched.
    """
    by_index = {ln["index"]: ln for ln in plan["lines"]}
    apply_dispositions = {AUTO, SECTION}
    if apply_reviews:
        apply_dispositions |= {REVIEW}

    new_list: List[Dict[str, Any]] = []
    applied = 0
    for idx, ing in enumerate(raw_ings):
        ln = by_index.get(idx)
        if ln is None or ln["disposition"] not in apply_dispositions:
            new_list.append(ing)
            continue

        if ln["disposition"] == SECTION:
            new_list.append(
                {
                    **ing,
                    "title": ln.get("section_title") or _line_text(ing),
                    "food": None,
                    "unit": None,
                    "quantity": 0,
                    "note": "",
                }
            )
            applied += 1
            continue

        # AUTO / REVIEW: one or more structured entries replace this line.
        emitted = 0
        for j, prop in enumerate(ln["proposals"]):
            if not prop["ok"]:
                continue
            food_obj = ensure_food(prop["food"], prop.get("food_id"))
            if not food_obj:
                continue
            unit_obj = ensure_unit(prop["unit"]) if prop.get("unit") else None
            entry = {
                "quantity": prop["quantity"] or 0,
                "unit": unit_obj,
                "food": food_obj,
                "note": prop["note"],
                "originalText": prop["text"],
                "referenceId": ing.get("referenceId") if emitted == 0 else None,
                "title": ing.get("title") if emitted == 0 else None,
                "display": None,
            }
            new_list.append(entry)
            emitted += 1
        if emitted:
            applied += 1
        else:
            new_list.append(ing)  # nothing usable — leave the original

    return {"ingredients": new_list, "applied": applied}


class CleanupMixin:
    """Recipe-ingredient cleanup, wired to the live Mealie client."""

    def _food_index(self) -> tuple[List[str], Dict[str, str]]:
        foods = self.get_all_foods()
        names = [f["name"] for f in foods if f.get("name")]
        id_by_name = {norm(f["name"]): f["id"] for f in foods if f.get("name")}
        return names, id_by_name

    def _recipe_titles(self) -> set:
        try:
            resp = self.get_recipes(per_page=-1)
            items = resp.get("items", []) if isinstance(resp, dict) else []
            return {r.get("name", "") for r in items}
        except Exception:  # noqa: BLE001 — titles are a best-effort nicety
            return set()

    def plan_recipe_cleanup(
        self,
        slug: str,
        confidence: float = DEFAULT_CONFIDENCE,
        food_names: Optional[List[str]] = None,
        food_id_by_name: Optional[Dict[str, str]] = None,
        known_titles: Optional[set] = None,
    ) -> Dict[str, Any]:
        """Fetch a recipe and compute its cleanup plan (no mutation)."""
        recipe = self.get_recipe(slug)
        raw_ings = recipe.get("recipeIngredient") or []
        if food_names is None or food_id_by_name is None:
            food_names, food_id_by_name = self._food_index()
        if known_titles is None:
            known_titles = self._recipe_titles()
        plan = build_plan(
            raw_ings,
            parse_fn=lambda texts: self.parse_ingredients(texts),
            food_names=food_names,
            food_id_by_name=food_id_by_name,
            known_titles=known_titles,
            confidence=confidence,
        )
        plan["slug"] = slug
        plan["name"] = recipe.get("name") or slug
        return plan

    def apply_recipe_cleanup(
        self,
        slug: str,
        confidence: float = DEFAULT_CONFIDENCE,
        apply_reviews: bool = False,
        plan: Optional[Dict[str, Any]] = None,
        food_names: Optional[List[str]] = None,
        food_id_by_name: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Compute (or accept) a plan and PATCH the recipe with the fixes."""
        recipe = self.get_recipe(slug)
        raw_ings = recipe.get("recipeIngredient") or []
        if food_names is None or food_id_by_name is None:
            food_names, food_id_by_name = self._food_index()
        if plan is None:
            plan = build_plan(
                raw_ings,
                parse_fn=lambda texts: self.parse_ingredients(texts),
                food_names=food_names,
                food_id_by_name=food_id_by_name,
                known_titles=self._recipe_titles(),
                confidence=confidence,
            )

        food_cache: Dict[str, Dict[str, Any]] = {}
        unit_cache: Dict[str, Dict[str, Any]] = {}
        created_foods: List[str] = []
        created_units: List[str] = []

        def ensure_food(name: str, food_id: Optional[str]) -> Optional[Dict[str, Any]]:
            if not name:
                return None
            key = norm(name)
            if key in food_cache:
                return food_cache[key]
            if food_id:
                obj = {"id": food_id, "name": name}
            else:
                obj = self.create_food(name)
                created_foods.append(name)
                food_id_by_name[key] = obj.get("id")
            food_cache[key] = obj
            return obj

        def ensure_unit(name: str) -> Optional[Dict[str, Any]]:
            if not name:
                return None
            key = norm(name)
            if key in unit_cache:
                return unit_cache[key]
            obj = self.create_unit(name)
            created_units.append(name)
            unit_cache[key] = obj
            return obj

        result = apply_plan(
            raw_ings, plan, ensure_food, ensure_unit, apply_reviews=apply_reviews
        )
        patched = False
        if result["applied"]:
            self.patch_recipe(slug, {"recipeIngredient": result["ingredients"]})
            patched = True

        return {
            "slug": slug,
            "name": plan.get("name", slug),
            "applied": result["applied"],
            "patched": patched,
            "created_foods": created_foods,
            "created_units": created_units,
            "counts": plan["counts"],
        }
