import base64
import logging
import os

logger = logging.getLogger("mealie-mcp")


def register_image_gen_tools(mcp, mealie):
    @mcp.tool()
    def generate_recipe_image(slug: str, details: str = "") -> dict:
        """Generate a food photo with OpenAI gpt-image and set it as the recipe image.

        Use this when a recipe has no image. It creates a realistic, appetizing
        photograph of the finished dish and uploads it to the recipe in Mealie.

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
