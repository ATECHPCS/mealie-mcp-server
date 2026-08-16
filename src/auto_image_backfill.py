import logging
import os
import time
from datetime import datetime, timezone

from tools.image_gen_tools import generate_recipe_image_core

logger = logging.getLogger("mealie-mcp")


def _parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no", "off")


def run_backfill_pass(mealie, delay_minutes: float, window_minutes: float, now: datetime = None) -> list:
    """Generate images for recently-created recipes that are still imageless
    once they are older than delay_minutes -- giving a manual-upload window.

    Recipes are scanned newest-first and capped at window_minutes old, so a
    recipe that never gets backfilled here (e.g. the pass was down) is not
    retried forever; the one-off full-catalog audit already covers the
    pre-existing backlog. Returns the list of per-recipe results acted on.
    """
    now = now or datetime.now(timezone.utc)
    acted = []
    data = mealie.get_recipes(order_by="createdAt", order_direction="desc", per_page=50)
    for item in (data or {}).get("items", []):
        created = _parse_iso(item.get("createdAt"))
        if not created:
            continue
        age_minutes = (now - created).total_seconds() / 60
        if age_minutes > window_minutes:
            # Newest-first order means everything after this is even older.
            break
        if age_minutes < delay_minutes:
            continue

        recipe_id = item.get("id")
        slug = item.get("slug")
        if not recipe_id or not slug:
            continue
        if mealie.has_image(recipe_id):
            continue

        logger.info(
            {
                "message": "Auto image backfill: generating after grace window",
                "slug": slug,
                "age_minutes": round(age_minutes, 1),
            }
        )
        result = generate_recipe_image_core(mealie, slug)
        if result.get("error"):
            logger.error({"message": "Auto image backfill failed", "slug": slug, "error": result["error"]})
        acted.append({"slug": slug, **result})
    return acted


def auto_image_backfill_loop(mealie, sleep=time.sleep):
    """Background loop: periodically auto-generate images for recipes that
    are still missing one AUTO_IMAGE_BACKFILL_DELAY_MINUTES after creation,
    so Ian has a window to upload his own photo before gpt-image fires.

    Plain blocking function meant to run in its own daemon thread (started
    from server.py before uvicorn.run()) rather than as an asyncio task tied
    to the ASGI app lifespan -- FastMCP's streamable_http_app() passes its
    own custom `lifespan=` to Starlette, which silently skips any
    @app.on_event("startup") handler registered afterward.
    """
    if not _env_bool("AUTO_IMAGE_BACKFILL_ENABLED", True):
        logger.info({"message": "Auto image backfill loop disabled (AUTO_IMAGE_BACKFILL_ENABLED=false)"})
        return

    delay_minutes = _env_float("AUTO_IMAGE_BACKFILL_DELAY_MINUTES", 5.0)
    poll_seconds = _env_float("AUTO_IMAGE_BACKFILL_POLL_SECONDS", 60.0)
    window_minutes = _env_float("AUTO_IMAGE_BACKFILL_WINDOW_MINUTES", 180.0)

    logger.info(
        {
            "message": "Auto image backfill loop started",
            "delay_minutes": delay_minutes,
            "poll_seconds": poll_seconds,
            "window_minutes": window_minutes,
        }
    )

    while True:
        try:
            run_backfill_pass(mealie, delay_minutes, window_minutes)
        except Exception as e:
            logger.error({"message": "Auto image backfill pass crashed", "error": str(e)})
        sleep(poll_seconds)
