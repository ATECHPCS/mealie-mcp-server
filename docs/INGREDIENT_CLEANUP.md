# Ingredient cleanup & food dedupe

Recipes scraped from the web arrive with **unstructured** ingredient lines —
raw text with no linked quantity/unit/food. Mealie's NLP parser
(`POST /api/parser/ingredients`) structures most lines well, but it has a small
set of reliable failure modes. This subsystem pre-processes lines into a form
the parser *can* handle, re-parses them, dedupes any new food against the
catalog, and decides whether each fix is safe to apply automatically or should
be held for a human.

## Pipeline

```
recipe.recipeIngredient
   │  (lines with text but no linked food)
   ▼
clean_line()            ← network-free pre-processor  (ingredient_cleanup.py)
   │  cleaned candidate text(s) + classification + auto_safe flag
   ▼
parse_ingredients()     ← Mealie NLP, now fed input it can parse
   │  quantity / unit / food + confidence
   ▼
find_duplicate()        ← reuse "water" instead of minting "hot water"  (food_dedupe.py)
   │
   ▼
disposition: auto | review | section | recipe_ref | skip     (cleanup.py)
```

`clean_line`, `build_plan`, and `apply_plan` are pure (the parser and catalog
are injected), so the behaviour below is covered by unit tests against the real
lines that were skipped in production.

## Line categories

| Category | Example | What happens |
|---|---|---|
| **normal** | `2 lb chicken thighs` | passed straight to the NLP parser |
| **parenthetical** | `1 ripe avocado, diced (about 150 g flesh)` | strip measure/conversion parentheticals & temperature tails, then parse |
| **compound** | `Salt and black pepper, to taste` | split into `salt` + `black pepper` (only when **both** sides are known seasonings — never `chicken and rice`) |
| **distributive** | `1 tsp each garlic powder and smoked paprika` | one entry per food, each carrying the shared `1 tsp` |
| **alternative** | `2 lb sirloin steak or shaved beef` | keep the first food, move `or shaved beef` to the note |
| **section_header** | `For the pineapple chimichurri:` | promote to a titled ingredient group (no food) |
| **recipe_reference** | `12 slices Keto Brioche Bread Recipe` | left raw — links to a sub-recipe are a manual UI action |

"Subtracting the quantity": for every category the quantity/unit is pulled into
its own fields and the leftover **food name** is what gets matched or created —
so the catalog gets `baby arugula`, not `2 cups baby arugula`.

## Dispositions — auto vs. review

A line is **auto** (applied on a non-dry-run) only when it is *both*:

1. **deterministic** (`auto_safe`) — the rewrite can't silently pick the wrong
   thing. Compounds, distributions, measure-noise stripping, `(or Y)`
   parentheticals, and section headers qualify.
2. **confident** — every cleaned candidate re-parses at/above the confidence
   threshold (default `0.85`) with a matched food that isn't a recipe reference.

Everything else is **review**:

- **Inline `X or Y` alternatives** are always review, even when the parse is
  confident — *which* alternative to keep is a judgement call, and the split can
  drop a shared head noun (`avocado or olive oil` → primary `avocado`). The plan
  still offers a best-guess primary + the alternative as a note, so accepting it
  is one click / `--apply-reviews`.
- **Low-confidence** parses and prose lines (`No ice in the 10 oz jars: …`).
- **recipe_ref** lines need a human to link the sub-recipe.

This is why running `cleanup_all_recipes` produces a `manual_review_queue`: it's
the short list of lines a human should look at, separated from the many that
were fixed automatically.

## Food dedupe

Imported recipes mint a food per unrecognised name, breeding near-duplicates.
`food_dedupe.find_duplicate` resolves a new name to an existing catalog food by,
in order: a curated `MERGE_OVERRIDES` map → exact normalised match → fuzzy match
at/above `AUTO_CUTOFF` (0.9). A curated `KEEP_DISTINCT` list protects
real-but-similar foods whose difference matters to a shopping list or macro
count — bell-pepper colours, ground-beef fat ratios, lemon vs lime,
chicken breast vs thighs.

`suggest_duplicate_clusters` (behind the `find_duplicate_foods` tool and
`--dupes`) is a looser, read-only pass for eyeballing borderline pairs before
merging them with Mealie's native `/api/foods/merge`.

## Safety

- **Dry-run by default** everywhere — MCP tools and CLI both preview unless told
  to apply.
- **Auto changes are conservative**; ambiguous ones are held.
- Foods are **deduped before creation**, and lines whose parsed food name
  contains "recipe" are never written back as a food.
- Applying a plan only ever **PATCHes `recipeIngredient`**; nothing else on the
  recipe is touched, and structured lines (already linked to a food) are ignored.
