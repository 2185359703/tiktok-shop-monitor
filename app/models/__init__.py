"""Import this package before accessing Base.metadata so all tables are registered."""

from .base import Base
from .change_event import ChangeEvent
from .enums import (
    AvailabilityStatus, ChangeEventType, ResolutionStatus, ResourceType,
    SeedType, ShopStatus, StockStatus, TaskStatus, TaskType,
)
from .product import Product, ProductVariant
from .product_image import ProductImage
from .product_content import ProductContent
from .matching import MatchStatus, ProductMatch, SkuMatch
from .seed import SeedShopObservation, ShopSeedLink
from .shop import Shop
from .snapshot import ProductSnapshot, VariantSnapshot
from .task import CrawlAttempt, CrawlTask

__all__ = [
    "Base", "SeedShopObservation", "Shop", "ShopSeedLink", "Product",
    "ProductVariant", "ProductSnapshot", "VariantSnapshot", "ChangeEvent",
    "CrawlTask", "CrawlAttempt", "SeedType", "ResolutionStatus", "ShopStatus",
    "StockStatus", "AvailabilityStatus", "ChangeEventType", "TaskType",
    "ResourceType", "TaskStatus",
    "ProductImage", "ProductMatch", "SkuMatch", "MatchStatus",
    "ProductContent",
]
