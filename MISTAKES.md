# MISTAKES.md

## Enforced Rules (check every task)

## Patterns (promote at 3 hits)

## Observations (first sightings)
- 2026-09-29: Compared a freshly imported recipe's ingredients before cleanup — Mealie stores imports as raw lines ("1 cup almond flour") and only the cleanup creates foods, so a real duplicate slipped through → run duplicate/ingredient comparisons on the cleaned recipe. (hits: 1)
- 2026-09-29: Treated "couldn't compare" (fetch error, empty ingredient sets) as a duplicate match, which justified deleting a new recipe → unknown is never evidence for a destructive action; keep and warn. (hits: 1)
- 2026-09-29: URL normalization dropped the whole query string, so youtube.com/watch?v=A == ?v=B → strip only tracking params (utm_*, fbclid, si...). Also 16 recipes share orgURL facebook.com/saved: URLs shared by 2+ recipes are not identities. (hits: 1)
- 2026-09-29: AI parser results were trusted after checking only food.name; a malformed confidence/unit crashed build_plan → validate every field downstream code reads before replacing an NLP result. (hits: 1)
- 2026-10-07: Left FastMCP streamable-HTTP in its default stateful mode; the SDK reaps sessions after 30 idle minutes, so a long-lived client (ZeroClaw daemon) got a 404 on its first call after a quiet spell → keep `stateless_http = True` on the HTTP transport; when debugging "bridge unreachable", grep the bridge log for `idle timeout` and `404` before blaming the network. (hits: 1)
