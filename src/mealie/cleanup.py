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

from .food_dedupe import norm, resolve_existing
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


def _referenced_ids(recipe: Dict[str, Any]) -> set:
    """referenceIds that a recipe's instruction steps link to.

    Expanding such a line (compound/distributive -> several rows) would leave
    the step pointing at only the first fragment, so those lines are held.
    """
    out: set = set()
    for step in recipe.get("recipeInstructions") or []:
        for ref in (step or {}).get("ingredientReferences") or []:
            rid = (ref or {}).get("referenceId")
            if rid:
                out.add(rid)
    return out


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
    referenced_ids: Optional[set] = None,
) -> Dict[str, Any]:
    """Produce a cleanup plan for a recipe's ingredient list (no mutation).

    Args:
        raw_ings: the recipe's recipeIngredient list.
        parse_fn: callable taking a list of raw strings and returning Mealie
            ParsedIngredient dicts, in order (i.e. `mealie.parse_ingredients`).
        food_names: catalog food names (kept for API compatibility; the
            auto-path resolves foods by exact/override only, not fuzzy).
        food_id_by_name: normalised-name -> food id, to resolve an existing food.
        known_titles: recipe titles, so sub-recipe references are detected.
        confidence: minimum NLP average confidence to accept a parse.
        referenced_ids: referenceIds pointed at by recipe instructions; a line
            with one of these is never auto-expanded (expansion would orphan the
            step's ingredient link).

    Returns:
        A plan dict with per-line dispositions and proposed structured entries.
    """
    food_names = food_names or []
    food_id_by_name = food_id_by_name or {}
    referenced_ids = referenced_ids or set()

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
    # The parser must return exactly one result per input, in order — we
    # correlate positionally. A short/misaligned response would silently assign
    # the wrong food to a line, so if the count is off we refuse to auto-apply
    # anything and downgrade every parsed line to review.
    aligned = len(parsed) == len(to_parse)

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
        line_parsed = parsed[lo:hi] if aligned else []
        proposals = []
        all_ok = True
        # If the parser response was short/misaligned, or this line's own slice
        # doesn't line up with its candidates, we can't trust the correlation.
        line_aligned = aligned and len(line_parsed) == len(cl.candidates)
        for cand, p in zip(cl.candidates, line_parsed):
            ing_obj = (p or {}).get("ingredient") or {}
            food = ing_obj.get("food") or {}
            unit = ing_obj.get("unit") or {}
            conf = ((p or {}).get("confidence") or {}).get("average") or 0
            # verify the parser echoed back the input we sent for this slot
            input_ok = (p or {}).get("input") in (None, cand.text)
            food_name = food.get("name")
            food_id = food.get("id")
            food_source = "none"
            if food_name and not food_id:
                canonical, existing_id = resolve_existing(food_name, food_id_by_name)
                if existing_id:
                    food_id = existing_id
                    food_name = canonical
                    food_source = f"existing:{canonical}"
                else:
                    food_source = "new"
            elif food_id:
                food_source = "existing"

            is_recipe_ref = bool(food_name) and _is_recipe_ref_name(food_name)
            structurally_valid = bool(food_name) and not is_recipe_ref and input_ok
            ok = structurally_valid and conf >= confidence
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
                    "structurally_valid": structurally_valid,
                    "ok": ok,
                }
            )
        rec["proposals"] = proposals

        # Expanding a line that a recipe step references would orphan that link,
        # so hold multi-candidate expansions of referenced lines for review.
        ref_id = (raw_ings[entry["index"]] or {}).get("referenceId")
        expands = len(cl.candidates) > 1
        references_step = expands and ref_id in referenced_ids

        if proposals and all_ok and line_aligned and cl.auto_safe and not references_step:
            rec["disposition"] = AUTO
            counts[AUTO] += 1
        elif proposals:
            rec["disposition"] = REVIEW
            counts[REVIEW] += 1
            if references_step:
                rec["reason"] = "expands a line referenced by a recipe step — review"
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


def _food_entry(prop, food_obj, unit_obj, first, ref_id, title):
    """Build a schema-valid RecipeIngredient-Input row for a food line.

    The Input model makes `display`/`referenceId` non-null strings and
    `disableAmount` default to True, so we set `display=""`, omit an absent
    `referenceId`, and force `disableAmount=False` (otherwise Mealie hides the
    parsed quantity/unit). `isFood=True` marks it a food row.
    """
    entry: Dict[str, Any] = {
        "quantity": prop["quantity"] or 0,
        "unit": unit_obj,
        "food": food_obj,
        "note": prop["note"] or "",
        "isFood": True,
        "disableAmount": False,
        "display": "",
    }
    if prop.get("text"):
        entry["originalText"] = prop["text"]
    # Preserve the source line's step-link + section title, but only on the
    # first emitted entry (inserted rows get a server-assigned referenceId).
    if first:
        if ref_id:
            entry["referenceId"] = ref_id
        if title is not None:
            entry["title"] = title
    return entry


def _section_entry(title, ref_id):
    """A titled section header row: no food, amount disabled, display cleared."""
    entry: Dict[str, Any] = {
        "title": title,
        "food": None,
        "unit": None,
        "quantity": 0,
        "note": "",
        "isFood": False,
        "disableAmount": True,
        "display": "",
    }
    if ref_id:
        entry["referenceId"] = ref_id
    return entry


def apply_plan(
    raw_ings: List[Dict[str, Any]],
    plan: Dict[str, Any],
    ensure_food: Callable[[str, Optional[str]], Optional[Dict[str, Any]]],
    ensure_unit: Callable[[str, Optional[str]], Optional[Dict[str, Any]]],
    apply_reviews: bool = False,
) -> Dict[str, Any]:
    """Build the new recipeIngredient list from a plan.

    Args:
        raw_ings: the original recipeIngredient list.
        plan: output of `build_plan`.
        ensure_food: (name, food_id) -> food object to link (create/reuse).
        ensure_unit: (name, unit_id) -> unit object to link (create/reuse), or None.
        apply_reviews: also apply REVIEW lines, using structurally-valid
            proposals even when their confidence was below the auto threshold.

    Returns:
        {"ingredients": [...new list...], "applied": n}. Lines left for review
        (when not opted in) or manual linking keep their original entry.
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
                _section_entry(
                    ln.get("section_title") or _line_text(ing),
                    ing.get("referenceId"),
                )
            )
            applied += 1
            continue

        # AUTO applies confident proposals; an opted-in REVIEW applies every
        # structurally-valid proposal regardless of the confidence threshold.
        review = ln["disposition"] == REVIEW
        emitted = 0
        for prop in ln["proposals"]:
            usable = prop["structurally_valid"] if review else prop["ok"]
            if not usable:
                continue
            food_obj = ensure_food(prop["food"], prop.get("food_id"))
            if not food_obj:
                continue
            unit_obj = (
                ensure_unit(prop["unit"], prop.get("unit_id"))
                if prop.get("unit")
                else None
            )
            new_list.append(
                _food_entry(
                    prop, food_obj, unit_obj,
                    first=(emitted == 0),
                    ref_id=ing.get("referenceId"),
                    title=ing.get("title"),
                )
            )
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
            referenced_ids=_referenced_ids(recipe),
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
                referenced_ids=_referenced_ids(recipe),
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

        def ensure_unit(name: str, unit_id: Optional[str]) -> Optional[Dict[str, Any]]:
            if not name:
                return None
            key = norm(name)
            if key in unit_cache:
                return unit_cache[key]
            if unit_id:
                obj = {"id": unit_id, "name": name}  # reuse the unit the parser matched
            else:
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
