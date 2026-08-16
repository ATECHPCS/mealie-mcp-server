"""MCP tools for cleaning up messy imported recipe ingredients + deduping foods.

These expose the `CleanupMixin` / `food_dedupe` logic so an agent can:
  * preview or apply a per-recipe ingredient cleanup (split compounds, strip
    measure noise, resolve "X or Y" alternatives, dedupe foods), and
  * find and merge duplicate foods in the catalog.

Everything defaults to a dry-run/preview — a mutating call requires
`dry_run=False`.
"""

import logging
import traceback
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from mealie import MealieFetcher
from mealie.food_dedupe import suggest_duplicate_clusters

logger = logging.getLogger("mealie-mcp")


def register_cleanup_tools(mcp: FastMCP, mealie: MealieFetcher) -> None:
    """Register ingredient-cleanup and food-dedupe tools."""

    @mcp.tool()
    def cleanup_recipe_ingredients(
        slug: str,
        dry_run: bool = True,
        apply_reviews: bool = False,
        confidence: float = 0.85,
    ) -> Dict[str, Any]:
        """Re-parse and clean up a recipe's unstructured ingredient lines.

        For every ingredient that is still raw text with no linked food, this
        splits idiomatic compounds ("salt and pepper"), distributes shared
        amounts ("1 tsp each A, B, C"), strips measurement/parenthetical noise,
        lifts "X or Y" alternatives into a note keeping the first food, promotes
        section headers ("For the sauce:") to titled groups, flags sub-recipe
        references, re-parses the cleaned text with Mealie's NLP parser, and
        dedupes any new food name against the catalog.

        Each line gets a disposition: `auto` (deterministic + confident, applied
        when not a dry run), `review` (ambiguous — which alternative to keep, or
        low confidence — needs a human), `section` (header promoted to a title),
        or `recipe_ref` (needs a manual sub-recipe link in the UI).

        Args:
            slug: Recipe slug to clean up.
            dry_run: When True (default) only return the plan; nothing is
                written. Set False to PATCH the recipe with the `auto` fixes.
            apply_reviews: Also apply `review` lines using their best-guess
                first food. Off by default — review lines are meant for a human.
            confidence: Minimum NLP average confidence to accept a parse.

        Returns:
            The cleanup plan (dry run) or an apply summary with counts of what
            was written and which foods/units were created.
        """
        try:
            if dry_run:
                plan = mealie.plan_recipe_cleanup(slug, confidence=confidence)
                return {"dry_run": True, **plan}
            result = mealie.apply_recipe_cleanup(
                slug, confidence=confidence, apply_reviews=apply_reviews
            )
            return {"dry_run": False, **result}
        except Exception as e:  # noqa: BLE001
            msg = f"Error cleaning up recipe '{slug}': {str(e)}"
            logger.error({"message": msg})
            logger.debug({"message": "traceback", "traceback": traceback.format_exc()})
            raise ToolError(msg)

    @mcp.tool()
    def cleanup_all_recipes(
        dry_run: bool = True,
        apply_reviews: bool = False,
        confidence: float = 0.85,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Sweep every recipe that still has unstructured ingredients.

        Runs `cleanup_recipe_ingredients` across the whole library. Fetches the
        food catalog and recipe titles once and reuses them for speed. Returns a
        per-recipe roll-up plus a combined list of lines still needing a human
        (review + recipe references).

        Args:
            dry_run: Preview only (default). Set False to write `auto` fixes.
            apply_reviews: Also apply best-guess `review` lines. Off by default.
            confidence: Minimum NLP average confidence to accept a parse.
            limit: Only process the first N recipes (for a quick trial run).

        Returns:
            Totals, per-recipe results, and the aggregated manual-review queue.
        """
        try:
            food_names, food_id_by_name = mealie._food_index()
            known_titles = mealie._recipe_titles()
            summaries = mealie.get_recipes(per_page=-1)
            items = summaries.get("items", []) if isinstance(summaries, dict) else []
            if limit:
                items = items[:limit]

            totals = {"auto": 0, "review": 0, "section": 0, "recipe_ref": 0, "skip": 0}
            per_recipe = []
            review_queue = []
            written = 0
            for summ in items:
                slug = summ.get("slug")
                if not slug:
                    continue
                plan = mealie.plan_recipe_cleanup(
                    slug,
                    confidence=confidence,
                    food_names=food_names,
                    food_id_by_name=food_id_by_name,
                    known_titles=known_titles,
                )
                if plan["unstructured"] == 0:
                    continue
                for k in totals:
                    totals[k] += plan["counts"].get(k, 0)
                for ln in plan["lines"]:
                    if ln["disposition"] in ("review", "recipe_ref"):
                        review_queue.append(
                            {"slug": slug, "raw": ln["raw"],
                             "disposition": ln["disposition"], "reason": ln["reason"]}
                        )
                applied = 0
                if not dry_run:
                    res = mealie.apply_recipe_cleanup(
                        slug,
                        confidence=confidence,
                        apply_reviews=apply_reviews,
                        plan=plan,
                        food_names=food_names,
                        food_id_by_name=food_id_by_name,
                    )
                    applied = res["applied"]
                    written += applied
                per_recipe.append(
                    {"slug": slug, "unstructured": plan["unstructured"],
                     "counts": plan["counts"], "applied": applied}
                )

            return {
                "dry_run": dry_run,
                "recipes_with_unstructured": len(per_recipe),
                "totals": totals,
                "lines_written": written,
                "manual_review_queue": review_queue,
                "per_recipe": per_recipe,
            }
        except Exception as e:  # noqa: BLE001
            msg = f"Error during library cleanup sweep: {str(e)}"
            logger.error({"message": msg})
            logger.debug({"message": "traceback", "traceback": traceback.format_exc()})
            raise ToolError(msg)

    @mcp.tool()
    def find_duplicate_foods(cutoff: float = 0.82) -> Dict[str, Any]:
        """List near-duplicate food-name pairs in the catalog (read-only).

        Fuzzy-matches the whole food catalog and surfaces pairs at/above the
        cutoff, suppressing curated keep-distinct pairs (bell-pepper colours,
        ground-beef fat ratios, lemon vs lime). Use the pairs to drive
        `merge_foods`. Nothing is changed.

        Args:
            cutoff: Similarity threshold (0-1). Lower surfaces more, noisier.

        Returns:
            Candidate duplicate pairs with similarity scores and the catalog id
            for each name (so you can merge without another lookup).
        """
        try:
            foods = mealie.get_all_foods()
            id_by_name = {f["name"]: f["id"] for f in foods if f.get("name")}
            pairs = suggest_duplicate_clusters(list(id_by_name), cutoff=cutoff)
            return {
                "catalog_size": len(foods),
                "candidate_pairs": [
                    {"a": a, "a_id": id_by_name.get(a),
                     "b": b, "b_id": id_by_name.get(b), "score": score}
                    for a, b, score in pairs
                ],
            }
        except Exception as e:  # noqa: BLE001
            msg = f"Error finding duplicate foods: {str(e)}"
            logger.error({"message": msg})
            logger.debug({"message": "traceback", "traceback": traceback.format_exc()})
            raise ToolError(msg)

    @mcp.tool()
    def merge_foods(
        from_food_id: str,
        to_food_id: str,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """Merge one food into another (repoints all recipes, deletes the dupe).

        Uses Mealie's native `/api/foods/merge`. Irreversible when applied, so
        it defaults to a dry run that just echoes what would happen.

        Args:
            from_food_id: UUID of the duplicate to remove.
            to_food_id: UUID of the canonical food to keep.
            dry_run: When True (default) do not merge — only report the intent.

        Returns:
            The intended or performed merge.
        """
        try:
            if dry_run:
                return {"dry_run": True, "from": from_food_id, "to": to_food_id,
                        "note": "set dry_run=False to perform the merge"}
            mealie.merge_foods(from_food_id, to_food_id)
            return {"dry_run": False, "merged_from": from_food_id, "into": to_food_id}
        except Exception as e:  # noqa: BLE001
            msg = f"Error merging foods {from_food_id} -> {to_food_id}: {str(e)}"
            logger.error({"message": msg})
            logger.debug({"message": "traceback", "traceback": traceback.format_exc()})
            raise ToolError(msg)
