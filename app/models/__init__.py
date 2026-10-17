"""Import this package before accessing Base.metadata so all tables are registered."""

from .base import Base
from .analytics_read import ProductReadModel, EventReadModel, AnalyticsRefreshRequest, AnalyticsSnapshot
from .change_event import ChangeEvent
from .enums import (
    AvailabilityStatus, ChangeEventType, ResolutionStatus, ResourceType,
    SeedType, ShopStatus, StockStatus, TaskStatus, TaskType,
)
from .product import Product, ProductVariant
from .product_image import ProductImage
from .product_content import ProductContent
from .product_review import ProductReview
from .matching import MatchStatus, ProductMatch, SkuMatch
from .seed import SeedShopObservation, ShopSeedLink
from .shop import Shop
from .shop_profile import ShopProfileSnapshot
from .snapshot import ProductSnapshot, VariantSnapshot
from .task import CrawlAttempt, CrawlTask
from .collection import CollectionControl, CollectionPlan, CollectionRun, CollectionRunTask, CollectionOperation
from .collection_launch import CollectionLaunch, CollectionLaunchTask
from .collection_monitor import CollectionAttemptFact, CollectionMinuteRollup, CollectionMonitorState
from .collection_alerts import CollectionAlert, CollectionAlertEvent, CollectionAlertEvaluation

__all__ = [
    "Base", "SeedShopObservation", "Shop", "ShopSeedLink", "Product",
    "ProductVariant", "ProductSnapshot", "VariantSnapshot", "ChangeEvent",
    "CrawlTask", "CrawlAttempt", "SeedType", "ResolutionStatus", "ShopStatus",
    "StockStatus", "AvailabilityStatus", "ChangeEventType", "TaskType",
    "ResourceType", "TaskStatus",
    "ProductImage", "ProductMatch", "SkuMatch", "MatchStatus",
    "ProductContent", "ShopProfileSnapshot", "ProductReview",
    "ProductReadModel", "EventReadModel", "AnalyticsRefreshRequest", "AnalyticsSnapshot",
    "CollectionControl", "CollectionPlan", "CollectionRun", "CollectionRunTask", "CollectionOperation",
    "CollectionLaunch", "CollectionLaunchTask",
    "CollectionAttemptFact", "CollectionMinuteRollup", "CollectionMonitorState",
    "CollectionAlert", "CollectionAlertEvent", "CollectionAlertEvaluation",
]
