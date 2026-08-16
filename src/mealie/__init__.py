from .categories import CategoriesMixin
from .cleanup import CleanupMixin
from .client import MealieClient
from .foods import FoodsMixin
from .group import GroupMixin
from .mealplan import MealplanMixin
from .parser import ParserMixin
from .recipe import RecipeMixin
from .shopping_list import ShoppingListMixin
from .tags import TagsMixin
from .tools import ToolsMixin
from .units import UnitsMixin
from .user import UserMixin


class MealieFetcher(
    CleanupMixin,
    RecipeMixin,
    CategoriesMixin,
    TagsMixin,
    FoodsMixin,
    UnitsMixin,
    ToolsMixin,
    ShoppingListMixin,
    MealplanMixin,
    ParserMixin,
    UserMixin,
    GroupMixin,
    MealieClient,
):
    pass
