import base64
import logging
import os

logger = logging.getLogger("mealie-mcp")


def register_image_gen_tools(mcp, mealie):
    @mcp.tool()
    def generate_recipe_image(slug: str, details: str = "", force: bool = False) -> dict:
        """Generate a food photo with OpenAI gpt-image and set it as the recipe image.

        Safe to call after every import: it checks whether a REAL image file
        already exists and only generates when one is missing (the recipe.image
        field is unreliable, so do not gate on it yourself). Creates a realistic,
        appetizing photograph of the finished dish and uploads it to Mealie.

        Args:
            slug: The recipe slug to add an image to.
            details: Optional extra guidance for the photo (key ingredients or
                plating). If empty, the recipe name and description are used.
        """
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return {"error": "OPENAI_API_KEY is not configured on the mealie bridge."}
        try:
            recipe = mealie.get_recipe(slug)
        except Exception as e:
            return {"error": f"Could not load recipe '{slug}': {e}"}
        recipe_id = recipe.get("id") if isinstance(recipe, dict) else None
        if not force and recipe_id and mealie.has_image(recipe_id):
            return {
                "skipped": True,
                "slug": slug,
                "message": "Recipe already has a real image, not regenerating (pass force=true to override).",
            }
        name = (recipe.get("name") or slug) if isinstance(recipe, dict) else slug
        desc = details or (recipe.get("description") if isinstance(recipe, dict) else "") or ""
        prompt = (
            f"A professional, appetizing food photograph of {name}. {desc} "
            "Natural soft lighting, shallow depth of field, plated on a clean "
            "neutral surface, realistic, high detail, no text and no watermark."
        ).strip()
        model = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
        size = os.getenv("OPENAI_IMAGE_SIZE", "1024x1024")
        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key)
            result = client.images.generate(model=model, prompt=prompt, size=size, n=1)
            b64 = result.data[0].b64_json
            if not b64:
                return {"error": "Image model returned no image data."}
            image_bytes = base64.b64decode(b64)
        except Exception as e:
            return {"error": f"Image generation failed ({model}): {e}"}
        try:
            mealie.update_recipe_image_bytes(slug, image_bytes, "png")
        except Exception as e:
            return {"error": f"Image generated but upload to Mealie failed: {e}"}
        logger.info({"message": "Generated and set recipe image", "slug": slug, "model": model})
        return {"ok": True, "slug": slug, "message": f"Generated and set an image for '{name}'."}


    @mcp.tool()
    def set_recipe_nutrition(
        slug: str,
        servings: int,
        calories: float,
        protein_g: float,
        carbs_g: float,
        fat_g: float,
    ) -> dict:
        """Set a recipe's servings and per-serving nutrition (macros).

        Values are PER SINGLE SERVING. Mealie stores nutrition as bare number
        strings with no units, so "42" not "42 g". Use this after import to add
        the calories/protein/carbs/fat that meal planning relies on.

        Args:
            slug: Recipe slug.
            servings: Number of servings the recipe makes (must be a real number).
            calories: Calories per serving.
            protein_g: Protein grams per serving.
            carbs_g: Carbohydrate grams per serving.
            fat_g: Fat grams per serving.
        """
        def _num(x):
            x = float(x)
            return str(int(x)) if x.is_integer() else str(round(x, 1))

        payload = {
            "recipeServings": servings,
            "nutrition": {
                "calories": _num(calories),
                "proteinContent": _num(protein_g),
                "carbohydrateContent": _num(carbs_g),
                "fatContent": _num(fat_g),
            },
        }
        try:
            mealie.patch_recipe(slug, payload)
        except Exception as e:
            return {"error": f"Failed to set nutrition for '{slug}': {e}"}
        return {"ok": True, "slug": slug, "servings": servings}
