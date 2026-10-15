from enum import StrEnum


class SeedType(StrEnum):
    SHOP_NAME = "shop_name"
    SHOP_URL = "shop_url"
    SHOP_ID = "shop_id"


class ResolutionStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    FAILED = "failed"


class ShopStatus(StrEnum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    INACTIVE = "inactive"


class AvailabilityStatus(StrEnum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    REMOVED = "removed"


class StockStatus(StrEnum):
    UNKNOWN = "unknown"
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    PARTIAL = "partial"


class ChangeEventType(StrEnum):
    PRODUCT_NEW = "product_new"
    PRICE_CHANGED = "price_changed"
    DISCOUNT_STARTED = "discount_started"
    DISCOUNT_ENDED = "discount_ended"
    STOCKOUT = "stockout"
    RESTOCKED = "restocked"
    VARIANT_CHANGED = "variant_changed"
    CONTENT_CHANGED = "content_changed"
    PRODUCT_REMOVED = "product_removed"


class TaskType(StrEnum):
    SHOP_DISCOVERY = "shop_discovery"
    PRODUCT_DISCOVERY = "product_discovery"
    PRODUCT_DETAIL = "product_detail"
    PRODUCT_SNAPSHOT = "product_snapshot"
    CHANGE_DETECTION = "change_detection"
    REPORT_GENERATION = "report_generation"


class ResourceType(StrEnum):
    SEED = "seed"
    SHOP = "shop"
    PRODUCT = "product"
    VARIANT = "variant"
    REPORT = "report"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAITING = "retry_waiting"
    SUCCEEDED = "succeeded"
    ABANDONED = "abandoned"
    DEAD = "dead"
    CANCELLED = "cancelled"
