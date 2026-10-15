"""Merchant content fidelity and safety; HTML fixtures are synthetic contracts."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from app.crawlers.product_content import CONTENT_SOURCE_VERSION, ProductContentProjection, parse_product_content, sanitize_content_source


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


def test_optional_html_cell_closures_preserve_size_table_rows_and_units():
    value = parse_product_content(info('<table><caption>Measurements (inch)</caption>'
        '<tr><th>Size<th>Bust<tr><td>S<td>40.1<tr><td>M<td>42.0</table>'))
    nodes = value.sections["description"]
    assert nodes[0]["text"] == "Measurements (inch)"
    assert [[cell["text"] for cell in row] for row in nodes[1]["rows"]] == [["Size", "Bust"], ["S", "40.1"], ["M", "42.0"]]
    assert value.completeness == "complete"


def test_ordered_list_start_and_omitted_item_closures_keep_original_numbering():
    value = parse_product_content(info('<ol start="-2"><li>Small<li>Medium</ol>'))
    assert value.sections["description"] == [{"type": "list", "ordered": True, "start": -2, "items": ["Small", "Medium"]}]
    assert value.completeness == "complete"


def test_list_images_and_following_text_are_kept_in_source_order_and_marked_partial():
    value = parse_product_content(info('<ul><li>First</li><li>Before<img src="https://cdn.example/size.jpg">After</li><li>Last</li></ul>'))
    nodes = value.sections["description"]
    assert [node["type"] for node in nodes] == ["list", "paragraph", "image", "paragraph", "list"]
    assert nodes[0]["items"] == ["First"] and nodes[1]["text"] == "Before"
    assert nodes[3]["text"] == "After" and nodes[4]["items"] == ["Last"]
    assert value.completeness == "partial"


def test_list_source_blocks_do_not_discard_embedded_size_images_or_valid_other_items():
    value = parse_product_content({"specifications": [], "desc_blocks": [{"type": "ul", "content": [
        "First item", '<p>Guide</p><img src="https://cdn.example/guide.jpg">', None, "Last item"]}]})
    nodes = value.sections["description"]
    assert [node["type"] for node in nodes] == ["list", "paragraph", "image", "list"]
    assert nodes[0]["items"] == ["First item"] and nodes[-1]["items"] == ["Last item"]
    assert value.completeness == "partial" and value.unsupported_blocks


def test_table_and_heading_images_are_retained_without_claiming_original_layout_complete():
    value = parse_product_content(info('<h2>Guide<img src="https://cdn.example/heading.jpg"></h2>'
        '<table><tr><td>Size image<img src="https://cdn.example/table.jpg"></td></tr></table><p>End</p>'))
    nodes = value.sections["description"]
    assert [node["type"] for node in nodes] == ["heading", "image", "table", "image", "paragraph"]
    assert [node["url"] for node in nodes if node["type"] == "image"] == ["https://cdn.example/heading.jpg", "https://cdn.example/table.jpg"]
    assert value.completeness == "partial" and "End" in value.description_text


@pytest.mark.parametrize("mutation", ["status_type", "node_type", "field_kind", "section_status", "field_presence", "count", "hidden_content"])
def test_replay_rejects_malformed_types_and_contradictory_coverage_with_value_error(mutation):
    record = asdict(parse_product_content(info("Actual merchant text")))
    if mutation == "status_type":
        record["completeness"] = []
    elif mutation == "node_type":
        record["sections"]["description"][0]["type"] = []
    elif mutation == "field_kind":
        record["source_shape"]["desc_blocks"]["kind"] = []
    elif mutation == "section_status":
        record["source_shape"]["sections"]["description"] = []
    elif mutation == "field_presence":
        record["source_shape"]["desc_blocks"]["present"] = False
    elif mutation == "count":
        record["source_shape"]["desc_blocks"]["count"] = 7
    else:
        record["source_shape"]["sections"]["description"] = "empty"
    with pytest.raises(ValueError):
        ProductContentProjection.from_record(record)


def test_null_absent_and_explicit_empty_keep_distinct_source_evidence():
    absent = parse_product_content({})
    null = parse_product_content({"specifications": None, "desc_blocks": None})
    empty = parse_product_content({"specifications": [], "desc_blocks": []})
    assert absent.source_shape["desc_blocks"]["kind"] == "missing"
    assert null.source_shape["desc_blocks"]["kind"] == "null"
    assert empty.source_shape["desc_blocks"]["kind"] == "list"
    assert absent.completeness == null.completeness == "missing" and empty.completeness == "empty"


def test_sanitized_evidence_excludes_scripts_private_components_and_events_but_replays_safe_sections():
    source = info('<p onclick="do-not-save-secret()">Cotton coat</p><script>do-not-save-secret</script>'
        '<style>do-not-save-secret</style><table><caption>Measurements (inch)</caption>'
        '<tr><th>Size<th>Bust<tr><td>S<td>40.1</table>'
        '<img src="https://cdn.example/details.jpg" onerror="do-not-save-secret()" width="800" height="1600">')
    source["review_info"] = {"buyer": "do-not-save-secret"}
    source["shipping"] = {"warehouse": "do-not-save-secret"}
    source["desc_blocks"][0]["private_session"] = "do-not-save-secret"
    clean = sanitize_content_source(source)
    serialized = json.dumps(clean)
    assert set(clean) == {"specifications", "desc_blocks"}
    assert all(value not in serialized for value in ("do-not-save-secret", "onclick", "onerror", "<script", "<style", "warehouse", "private_session"))
    assert parse_product_content(clean).sections == parse_product_content(source).sections
    assert parse_product_content(source).completeness == "partial"


def test_unknown_evidence_objects_only_retain_field_names_and_types_and_enforce_size_budget():
    source = {"desc_blocks": [{"type": "image", "content": {"cookie": "do-not-save-secret", "nested": {"private": "do-not-save-secret"}}}]}
    clean = sanitize_content_source(source)
    assert "do-not-save-secret" not in json.dumps(clean)
    assert clean["desc_blocks"][0]["content"]["unsupported_shape"]["fields"] == {"cookie": "string", "nested": "object"}
    assert parse_product_content(clean).completeness == "partial"
    assert sanitize_content_source(info("x" * 1_000_001)) is None


def test_source_sanitizer_preserves_absent_null_and_empty_fields_without_new_keys():
    assert sanitize_content_source({}) == {}
    assert sanitize_content_source({"desc_blocks": None}) == {"desc_blocks": None}
    assert sanitize_content_source({"desc_blocks": []}) == {"desc_blocks": []}


def test_nested_measurement_table_is_retained_and_marked_as_layout_degradation():
    value = parse_product_content(info('<table><tr><td>Size guide<table><caption>Units: cm</caption>'
        '<tr><th>Size</th><th>Bust</th></tr><tr><td>S</td><td>102</td></tr></table></td></tr></table>'))
    tables = [node for node in value.sections["description"] if node["type"] == "table"]
    assert len(tables) == 2
    assert [[cell["text"] for cell in row] for row in tables[-1]["rows"]] == [["Size", "Bust"], ["S", "102"]]
    assert value.completeness == "partial" and "cm" in value.description_text


def test_nested_table_images_and_captions_are_not_duplicated_by_outer_layout_table():
    value = parse_product_content(info('<table><tr><td><table><caption>Unique units: inch</caption>'
        '<tr><td>S<img src="https://cdn.example/unique-chart.jpg"></td></tr></table></td></tr></table>'))
    nodes = value.sections["description"]
    assert sum(node["type"] == "image" for node in nodes) == 1
    assert sum(node["type"] == "heading" and node["text"] == "Unique units: inch" for node in nodes) == 1
    assert value.completeness == "partial"
