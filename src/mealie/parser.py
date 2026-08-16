import logging
from typing import Any, Dict, List

logger = logging.getLogger("mealie-mcp")


class ParserMixin:
    """Mixin class for Mealie's ingredient-parser endpoint (/api/parser)."""

    def parse_ingredients(
        self, ingredients: List[str], parser: str = "nlp"
    ) -> List[Dict[str, Any]]:
        """Parse raw ingredient lines into structured quantity/unit/food.

        Args:
            ingredients: Raw ingredient text, e.g. "2 lb boneless chicken thighs"
            parser: Registered Mealie parser to use ("nlp" or "brute")

        Returns:
            List[Dict[str, Any]]: One result per input, in the same order, each
            with "input", "confidence", and a structured "ingredient" object.
        """
        if not ingredients:
            return []

        logger.info({"message": "Parsing ingredients", "count": len(ingredients)})
        return self._handle_request(
            "POST",
            "/api/parser/ingredients",
            json={"parser": parser, "ingredients": ingredients},
        )
