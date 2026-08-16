"""One-shot batch sweep to clean up unstructured recipe ingredients.

A thin CLI over `CleanupMixin`, meant for clearing the backlog of recipes
imported before the cleanup logic existed. Defaults to a dry run.

Usage (from the repo root, with MEALIE_BASE_URL / MEALIE_API_KEY set):

    uv run python -m src.cleanup_cli --dry-run           # preview everything
    uv run python -m src.cleanup_cli --apply             # write the `auto` fixes
    uv run python -m src.cleanup_cli --apply --apply-reviews   # also best-guess reviews
    uv run python -m src.cleanup_cli --dry-run --slug keto-cheese-bread   # one recipe

    # dedupe pass (read-only unless --merge given):
    uv run python -m src.cleanup_cli --dupes             # list near-duplicate foods
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv  # noqa: E402

from mealie import MealieFetcher  # noqa: E402
from mealie.food_dedupe import suggest_duplicate_clusters  # noqa: E402


def _connect() -> MealieFetcher:
    load_dotenv()
    base = os.getenv("MEALIE_BASE_URL")
    key = os.getenv("MEALIE_API_KEY")
    if not base or not key:
        sys.exit("Set MEALIE_BASE_URL and MEALIE_API_KEY (or put them in .env)")
    return MealieFetcher(base_url=base, api_key=key)


def _run_dupes(m: MealieFetcher, cutoff: float) -> None:
    foods = m.get_all_foods()
    names = [f["name"] for f in foods if f.get("name")]
    pairs = suggest_duplicate_clusters(names, cutoff=cutoff)
    print(f"Catalog size: {len(foods)}  |  candidate duplicate pairs: {len(pairs)}\n")
    for a, b, score in pairs:
        print(f"  [{score:.2f}]  {a!r}  <->  {b!r}")
    print("\nMerge with the merge_foods MCP tool or Mealie's UI (from -> to).")


def _run_cleanup(m: MealieFetcher, args: argparse.Namespace) -> None:
    food_names, food_id_by_name = m._food_index()
    known_titles = m._recipe_titles()

    if args.slug:
        slugs = [args.slug]
    else:
        resp = m.get_recipes(per_page=-1)
        slugs = [r["slug"] for r in resp.get("items", []) if r.get("slug")]

    totals = {"auto": 0, "review": 0, "section": 0, "recipe_ref": 0, "skip": 0}
    review_lines = []
    written = 0
    created_foods: list[str] = []
    touched = 0

    for slug in slugs:
        plan = m.plan_recipe_cleanup(
            slug,
            confidence=args.confidence,
            food_names=food_names,
            food_id_by_name=food_id_by_name,
            known_titles=known_titles,
        )
        if plan["unstructured"] == 0:
            continue
        touched += 1
        for k in totals:
            totals[k] += plan["counts"].get(k, 0)
        for ln in plan["lines"]:
            if ln["disposition"] in ("review", "recipe_ref"):
                review_lines.append((slug, ln["disposition"], ln["raw"]))

        if args.apply:
            res = m.apply_recipe_cleanup(
                slug,
                confidence=args.confidence,
                apply_reviews=args.apply_reviews,
                plan=plan,
                food_names=food_names,
                food_id_by_name=food_id_by_name,
            )
            written += res["applied"]
            created_foods.extend(res["created_foods"])
            tag = f"applied={res['applied']}"
        else:
            tag = "DRY"
        print(
            f"[{tag}] {slug}: unstructured={plan['unstructured']} "
            f"auto={plan['counts']['auto']} review={plan['counts']['review']} "
            f"section={plan['counts']['section']} recipe_ref={plan['counts']['recipe_ref']}"
        )

    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"\n=== {mode} — {touched} recipes had unstructured ingredients ===")
    print(f"auto={totals['auto']} review={totals['review']} section={totals['section']} "
          f"recipe_ref={totals['recipe_ref']} skip={totals['skip']}")
    if args.apply:
        print(f"lines written: {written}  |  new foods created: {len(set(created_foods))}")
        if created_foods:
            print("  " + ", ".join(sorted(set(created_foods))))

    if review_lines:
        print(f"\n--- {len(review_lines)} lines need a human ---")
        for slug, disp, raw in review_lines:
            print(f"  ({disp:11}) {slug}: {raw}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Clean up unstructured recipe ingredients.")
    ap.add_argument("--dry-run", action="store_true", help="preview only (default)")
    ap.add_argument("--apply", action="store_true", help="write the auto fixes to Mealie")
    ap.add_argument("--apply-reviews", action="store_true",
                    help="also apply best-guess review lines")
    ap.add_argument("--slug", help="only process this recipe slug")
    ap.add_argument("--confidence", type=float, default=0.85,
                    help="minimum NLP average confidence (default 0.85)")
    ap.add_argument("--dupes", action="store_true", help="list near-duplicate foods and exit")
    ap.add_argument("--cutoff", type=float, default=0.82,
                    help="dupe similarity cutoff (default 0.82)")
    args = ap.parse_args()

    m = _connect()
    if args.dupes:
        _run_dupes(m, args.cutoff)
        return
    _run_cleanup(m, args)


if __name__ == "__main__":
    main()
