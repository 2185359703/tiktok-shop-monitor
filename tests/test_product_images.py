import json
from copy import deepcopy

import pytest

from app.crawlers.product_detail import DetailSnapshot, parse_detail_block
from app.crawlers.product_images import IMAGE_SOURCE_VERSION, ImageCandidate, image_candidates, valid_image_url
from app.crawlers.product_list import parse_product, parse_store_products
from test_product_detail import block, snapshot, SID, URL
from test_product_list import product, store_html, URL as STORE_URL


IMAGE = "https://p16-oec-general-useast5.ttcdn-us.com/photo.jpg~tplv.webp"
THUMB = "https://p16-oec-general-useast5.ttcdn-us.com/photo.jpg~thumb.webp"


def gallery():
    return [{"uri": "tos/public/photo-one", "url_list": [IMAGE, IMAGE.replace("p16", "p19")],
             "thumb_url_list": [THUMB]},
            {"uri": "tos/public/photo-two", "url_list": [IMAGE.replace("photo", "second")]},
            {"uri": "tos/public/photo-one", "url_list": [IMAGE.replace("p16", "p19")]}]


def test_gallery_keeps_order_deduplicates_mirrors_and_rejects_unsafe_urls():
    rows = gallery() + [{"uri": "bad", "url_list": ["javascript:alert(1)", "data:image/png;base64,x", "http://127.0.0.1/x"]}]
    images = image_candidates(rows)
    assert len(images) == 2 and [image.role for image in images] == ["cover", "gallery"]
    assert [image.position for image in images] == [0, 1]
    assert images[0].image_url == IMAGE and images[0].thumbnail_url == THUMB
    assert images[1].thumbnail_url is None
    assert all(image.source_complete is False for image in images)


def test_valid_duplicate_photo_uris_do_not_make_gallery_partial():
    images = image_candidates(gallery())
    assert len(images) == 2
    assert all(image.source_complete is True for image in images)


@pytest.mark.parametrize("invalid_row", [None, {}, {"url_list": "unexpected"},
                                         {"uri": "missing-photo", "url_list": []},
                                         {"uri": "bad-photo", "url_list": ["javascript:alert(1)"]}])
def test_unparsed_source_rows_mark_even_earlier_photos_partial(invalid_row):
    images = image_candidates([gallery()[0], invalid_row, gallery()[1]])
    assert len(images) == 2 and images[0].image_url == IMAGE
    assert all(image.source_complete is False for image in images)


def test_source_row_limit_is_not_reported_as_complete_gallery():
    rows = [{"uri": f"tos/public/photo-{index}", "url_list": [IMAGE.replace("photo", str(index))]}
            for index in range(101)]
    images = image_candidates(rows)
    assert len(images) == 100
    assert all(image.source_complete is False for image in images)


@pytest.mark.parametrize("value", [None, "false", 0, 1])
def test_projected_completeness_requires_a_boolean(value):
    record = {"image_key": "a" * 64, "image_url": IMAGE, "thumbnail_url": None,
              "role": "cover", "position": 0, "source_complete": value}
    with pytest.raises(ValueError):
        ImageCandidate.from_record(record)


@pytest.mark.parametrize("value", [None, "", "//cdn.example/photo.jpg", "file:///x", "http://localhost/x",
                                    "https://u:secret@cdn.example/x", "https://cdn.example/x\n",
                                    "http://10.1.1.1/x", "https://cdn.example:bad/x", "data:image/png,x"])
def test_invalid_image_urls_are_not_returned(value):
    assert valid_image_url(value) is None


def test_only_product_images_are_projected_from_detail_and_listing():
    value = block()
    value["product_info"]["images"] = gallery()
    value["review_info"] = {"image": {"url_list": ["https://private.example/customer.jpg"]}}
    detail = parse_detail_block(value, URL, SID)
    assert len(detail.images) == 2 and detail.images[0].image_url == IMAGE
    listing = product()
    listing["image"] = gallery()[0]
    listing["transparent_image"] = {"url_list": ["https://private.example/other.jpg"]}
    assert parse_product(listing, SID).images[0].image_key == detail.images[0].image_key


def test_cdn_mirror_changes_do_not_change_listing_business_duplicate_checks():
    first = product()
    first["image"] = gallery()[0]
    mirror = deepcopy(first)
    mirror["image"]["url_list"].reverse()
    feed = parse_store_products(store_html([first, mirror]), STORE_URL)
    assert len(feed.products) == 1 and feed.duplicate_products == 1


def test_old_detail_evidence_restores_without_images_and_new_evidence_validates(tmp_path):
    value = block()
    value["product_info"]["images"] = gallery()
    page = snapshot(value)
    record = json.loads(page.save_evidence(tmp_path).read_text("utf-8"))
    restored = DetailSnapshot.from_evidence(record)
    assert restored.detail.images == page.detail.images and record["image_source_version"] == IMAGE_SOURCE_VERSION
    old = deepcopy(record)
    old.pop("image_source_version")
    old["detail"].pop("images")
    assert DetailSnapshot.from_evidence(old).detail.images == ()
    legacy = deepcopy(record)
    for image in legacy["detail"]["images"]:
        image.pop("source_complete")
    legacy_images = DetailSnapshot.from_evidence(legacy).detail.images
    assert [image.image_key for image in legacy_images] == [image.image_key for image in page.detail.images]
    assert all(image.source_complete is False for image in legacy_images)
    record["detail"]["images"][0]["image_url"] = "javascript:alert(1)"
    with pytest.raises(ValueError):
        DetailSnapshot.from_evidence(record)
