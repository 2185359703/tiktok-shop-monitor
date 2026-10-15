"""Merchant content fidelity and safety; HTML fixtures are synthetic contracts."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from app.crawlers.product_content import CONTENT_SOURCE_VERSION, ProductContentProjection, parse_product_content


def info(body):
    return {"specifications": [{"name": "Fabric weight", "value": "Heavyweight"}],
            "desc_blocks": [{"type": "text", "text": body}]}


def test_text_lists_table_and_description_images_keep_source_order_without_executable_html():
    value = parse_product_content(info('<h2>Size guide (inch)</h2><p>Long <b>coat</b>.</p>'
        '<ul><li>Side pockets</li><li>Heavyweight</li></ul>'
        '<table><tr><th>Size</th><th>Bust (in)</th></tr><tr><td>S</td><td>40.1</td></tr></table>'
        '<img src="https://cdn.example/detail.webp" alt="Coat details" width="800" height="1600">'
        '<p>Wash cold.</p>'))
    nodes = value.sections["description"]
    assert [node["type"] for node in nodes] == ["heading", "paragraph", "list", "table", "image", "paragraph"]
    assert nodes[2] == {"type": "list", "ordered": False, "items": ["Side pockets", "Heavyweight"]}
    assert nodes[3]["rows"][0][1] == {"text": "Bust (in)", "header": True, "rowspan": 1, "colspan": 1}
    assert nodes[3]["rows"][1][1]["text"] == "40.1"
    assert nodes[4]["role"] == "description" and nodes[4]["height"] == 1600
    assert "Bust (in)" in value.description_text and value.completeness == "complete"


def test_unsafe_html_is_removed_and_external_components_never_enter_merchant_content():
    source = info('<p onclick="steal()">Merchant text</p><script>secret()</script>'
                  '<img src="http://127.0.0.1/private"><iframe>customer</iframe>')
    source["aigc_info"] = {"content": "inferred wool"}
    source["review_info"] = {"customer": "private buyer"}
    value = parse_product_content(source)
    serialized = json.dumps(asdict(value))
    assert value.description_text == "Merchant text" and value.completeness == "partial"
    assert all(marker not in serialized for marker in ("secret()", "steal()", "127.0.0.1", "inferred wool", "private buyer", "customer"))


def test_parameters_preserve_duplicates_and_are_separate_from_description():
    source = info("Material: Cotton")
    source["specifications"] = [{"name": "Material", "value": "Cotton"}, {"name": "Material", "value": "Polyester"}]
    value = parse_product_content(source)
    assert [row["value"] for row in value.sections["specifications"]] == ["Cotton", "Polyester"]
    assert value.description_text == "Material: Cotton"


def test_text_and_observed_content_fields_both_supported_without_guessing_unknown_images():
    value = parse_product_content({"specifications": [], "desc_blocks": [
        {"type": "text", "content": "First paragraph"},
        {"type": "ul", "content": ["Second item", "Third item"]},
        {"type": "image", "content": {"unverified_url_field": "https://cdn.example/a.webp"}}]})
    assert value.description_text == "First paragraph\nSecond item\nThird item"
    assert value.completeness == "partial"
    assert "https://cdn.example/a.webp" not in json.dumps(asdict(value))
    assert value.source_shape["desc_blocks"]["types"] == {"text": 1, "ul": 1, "image": 1}


@pytest.mark.parametrize("source,status", [({}, "missing"), ({"specifications": [], "desc_blocks": []}, "empty"),
    ({"specifications": [], "desc_blocks": [{"type": "text", "content": None}]}, "partial"),
    ({"specifications": [{"name": "Season", "value": "Winter"}]}, "partial")])
def test_missing_empty_partial_are_not_misreported_as_complete(source, status):
    assert parse_product_content(source).completeness == status


def test_projection_roundtrip_rejects_unsafe_urls_unknown_nodes_and_uncontrolled_metadata():
    record = asdict(parse_product_content(info('<img src="https://cdn.example/details.jpg">')))
    assert ProductContentProjection.from_record(json.loads(json.dumps(record))) == ProductContentProjection.from_record(record)
    record["sections"]["description"][0]["url"] = "javascript:alert(1)"
    with pytest.raises(ValueError):
        ProductContentProjection.from_record(record)
    record = asdict(parse_product_content(info("Safe merchant text")))
    record["source_shape"]["private_response"] = {"cookie": "not allowed"}
    with pytest.raises(ValueError):
        ProductContentProjection.from_record(record)
    record.pop("source_shape")
    with pytest.raises(ValueError):
        ProductContentProjection.from_record(record)


def test_large_optional_description_marks_partial_instead_of_failing_business_parser():
    value = parse_product_content(info("x" * 200_001))
    assert value.completeness == "partial" and value.description_text is None
    assert value.unsupported_blocks


def test_table_units_in_caption_and_cell_spans_are_preserved():
    value = parse_product_content(info('<table><caption>Measurements (inch)</caption>'
        '<tr><th colspan="2">Bust</th></tr><tr><td rowspan="2">S</td><td>40.1</td></tr></table>'))
    nodes = value.sections["description"]
    assert nodes[0]["text"] == "Measurements (inch)"
    assert nodes[1]["rows"][0][0]["colspan"] == 2
    assert nodes[1]["rows"][1][0]["rowspan"] == 2
    assert value.completeness == "complete"


def test_replay_cannot_claim_complete_coverage_for_partial_or_inconsistent_text():
    record = asdict(parse_product_content({"specifications": []}))
    record["completeness"] = "complete"
    with pytest.raises(ValueError):
        ProductContentProjection.from_record(record)
    record = asdict(parse_product_content(info("Actual merchant text")))
    record["description_text"] = "Different text"
    with pytest.raises(ValueError):
        ProductContentProjection.from_record(record)


def test_three_real_allowlisted_samples_preserve_observed_parameters_and_report_partial_blocks():
    samples = json.loads((Path(__file__).parent / "fixtures/product_contents.json").read_text("utf-8"))
    # These are public, allowlisted source projections, not a claim that table or
    # long-image platform fields have already been verified in production.
    for name, count, completeness in (("anrabess", 7, "complete"),
            ("dokotoo", 17, "complete"), ("chouyatou", 27, "partial")):
        source = samples[name]
        value = parse_product_content(source)
        assert len(value.sections["specifications"]) == count
        assert value.completeness == completeness and value.source_version == CONTENT_SOURCE_VERSION
        assert value.description_text

