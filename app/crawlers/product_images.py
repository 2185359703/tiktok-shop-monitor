"""Project public product photo fields, without downloading image files."""

import ipaddress
import re
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlsplit


IMAGE_SOURCE_VERSION = "public_product_images_v1"
MAX_IMAGES = 100


def valid_image_url(value):
    """Accept only absolute public HTTP(S) links from the platform image fields."""
    if not isinstance(value, str) or not 1 <= len(value) <= 8192 or re.search(r"[\s\x00-\x1f\x7f]", value):
        return None
    try:
        url = urlsplit(value)
        host = url.hostname
        if (url.scheme not in {"http", "https"} or not host or url.username or url.password or url.fragment
                or url.port not in {None, 80, 443} or not url.path or host.casefold() == "localhost"
                or "." not in host):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if not re.fullmatch(r"[a-zA-Z0-9.-]+", host) or host.endswith((".local", ".localhost", ".internal")):
                return None
        return value
    except ValueError:
        return None


def first_url(values):
    if not isinstance(values, list):
        return None
    return next((url for value in values[:16] if (url := valid_image_url(value))), None)


@dataclass(frozen=True)
class ImageCandidate:
    image_key: str
    image_url: str
    thumbnail_url: str | None
    role: str
    position: int

    @classmethod
    def from_record(cls, value):
        if (not isinstance(value, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("image_key", "")))
                or valid_image_url(value.get("image_url")) is None
                or (value.get("thumbnail_url") is not None and valid_image_url(value.get("thumbnail_url")) is None)
                or value.get("role") not in {"cover", "gallery"} or type(value.get("position")) is not int
                or not 0 <= value["position"] < MAX_IMAGES):
            raise ValueError("图片投影含无效链接或顺序。")
        return cls(**{key: value[key] for key in cls.__dataclass_fields__})


def image_candidates(rows):
    """One row per source photo URI; CDN mirrors and thumbnails are alternatives."""
    if not isinstance(rows, list):
        return ()
    images, keys = [], set()
    for row in rows[:MAX_IMAGES]:
        if not isinstance(row, dict) or not (url := first_url(row.get("url_list"))):
            continue
        uri = row.get("uri")
        identity = "uri:" + uri if isinstance(uri, str) and 0 < len(uri) <= 2048 else "url:" + url
        key = sha256(identity.encode("utf-8")).hexdigest()
        if key in keys:
            continue
        keys.add(key)
        images.append(ImageCandidate(key, url, first_url(row.get("thumb_url_list")),
                                     "cover" if not images else "gallery", len(images)))
    return tuple(images)


def detail_images(info):
    return image_candidates(info.get("images"))


def listing_images(product):
    image = product.get("image")
    return image_candidates([image]) if isinstance(image, dict) else ()
