"""Allowlisted merchant content as ordered, non-executable display nodes.

Only the already observed ``product_info.specifications`` and ``desc_blocks``
are inputs. An unsupported platform shape is evidence of partial coverage,
never permission to walk arbitrary page components or infer merchant facts.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from html import escape

from app.crawlers.product_images import valid_image_url


CONTENT_SOURCE_VERSION = "public_product_content_v1"
MAX_CONTENT_BYTES = 1_000_000
MAX_BLOCKS = 500
MAX_TEXT = 200_000
COMPLETENESS = {"missing", "empty", "partial", "complete"}
_SHAPE_KEYS = {"specifications", "desc_blocks", "sections", "issues"}
_BLOCKED = {"script", "style", "iframe", "object", "embed", "svg", "math", "form", "button", "input", "select", "textarea", "template", "noscript"}
_VOID = {"area", "base", "br", "col", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def _label(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) else "unknown"


def _text(value, maximum=MAX_TEXT):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ValueError("商品介绍文字结构无效。")
    return value


def _positive(value, maximum=100_000):
    return int(value) if isinstance(value, str) and 1 <= len(value) <= 6 and value.isascii() and value.isdigit() and 0 < int(value) <= maximum else None


def _list_start(value):
    return int(value) if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]{1,7}", value) and -1_000_000 <= int(value) <= 1_000_000 else None


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "root", "attrs": {}, "children": []}
        self.stack = [self.root]
        self.blocked = []
        self.issues = []
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        if self.blocked:
            if tag not in _VOID:
                self.blocked.append(tag)
            return
        if tag in _BLOCKED:
            self.issues.append("unsafe_html_removed")
            if tag not in _VOID:
                self.blocked.append(tag)
            return
        if len(self.stack) > 64:
            raise ValueError("商品介绍 HTML 嵌套过深。")
        self.nodes += 1
        if self.nodes > MAX_BLOCKS * 10:
            raise ValueError("商品介绍 HTML 节点过多。")
        # HTML allows omitted closing tags. Without these scoped closures a
        # measurement such as <td>S<td>40.1<tr> gets merged into the first cell.
        close = {"li": {"li"}, "p": {"p"}, "tr": {"tr"},
                 "td": {"td", "th"}, "th": {"td", "th"},
                 "thead": {"thead", "tbody", "tfoot"},
                 "tbody": {"thead", "tbody", "tfoot"},
                 "tfoot": {"thead", "tbody", "tfoot"}}
        boundaries = {"li": {"ul", "ol"}, "p": {"table", "li"},
                      "tr": {"table"}, "td": {"tr", "table"}, "th": {"tr", "table"},
                      "thead": {"table"}, "tbody": {"table"}, "tfoot": {"table"}}
        if tag in close:
            for index in range(len(self.stack) - 1, 0, -1):
                active = self.stack[index]["tag"]
                if active in close[tag]:
                    self.stack = self.stack[:index]
                    break
                if active in boundaries[tag]:
                    break
        allowed = {key: value for key, value in attrs if key in {"src", "alt", "width", "height", "rowspan", "colspan", "start"} and isinstance(value, str)}
        node = {"tag": tag, "attrs": allowed, "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.blocked:
            if tag in self.blocked:
                self.blocked = self.blocked[:self.blocked.index(tag)]
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index]["tag"] == tag:
                self.stack = self.stack[:index]
                return

    def handle_data(self, value):
        if not self.blocked:
            self.stack[-1]["children"].append(value)


def _flat(node):
    if isinstance(node, str):
        return node
    if node["tag"] == "br":
        return "\n"
    value = "".join(_flat(child) for child in node["children"])
    return value + "\n" if node["tag"] in {"p", "div", "li", "tr"} else value


def _descendants(node, tag):
    for child in node.get("children", []):
        if isinstance(child, dict):
            if child["tag"] == tag:
                yield child
            else:
                yield from _descendants(child, tag)


def _table_descendants(node, tag):
    for child in node.get("children", []):
        if isinstance(child, dict) and child["tag"] != "table":
            if child["tag"] == tag:
                yield child
            else:
                yield from _table_descendants(child, tag)


def _html_nodes(value):
    parser = _Tree()
    parser.feed(_text(value))
    parser.close()
    issues, nodes, buffer = list(parser.issues), [], []

    def flush():
        text = "".join(buffer).strip()
        buffer.clear()
        if text:
            nodes.append({"type": "paragraph", "text": text})

    def walk(children):
        for child in children:
            if isinstance(child, str):
                buffer.append(child)
                continue
            tag, attrs = child["tag"], child["attrs"]
            if tag == "br":
                buffer.append("\n")
            elif tag == "img":
                flush()
                url = valid_image_url(attrs.get("src"))
                if url is None:
                    issues.append("image_source_unsupported")
                    continue
                nodes.append({"type": "image", "url": url, "alt": attrs.get("alt", "")[:4000],
                              "role": "description", "width": _positive(attrs.get("width")),
                              "height": _positive(attrs.get("height"))})
            elif tag in {"ul", "ol"}:
                flush()
                items = []
                start = _list_start(attrs.get("start")) if tag == "ol" else None
                if "start" in attrs and start is None:
                    issues.append("list_start_unsupported")
                item_index = 0
                chunk_start = start
                def flush_list():
                    if items:
                        node = {"type": "list", "ordered": tag == "ol", "items": list(items)}
                        if tag == "ol" and chunk_start is not None:
                            node["start"] = chunk_start
                        nodes.append(node)
                        items.clear()
                for item in child["children"]:
                    if not isinstance(item, dict) or item["tag"] != "li":
                        if isinstance(item, str) and item.strip():
                            issues.append("list_unwrapped_text")
                            flush_list()
                            buffer.append(item)
                            flush()
                        continue
                    complex_item = any(_descendants(item, "img")) or any(_descendants(item, "table")) or any(_descendants(item, "ul")) or any(_descendants(item, "ol"))
                    if complex_item:
                        flush_list()
                        issues.append("complex_list_item")
                        walk(item["children"])
                        flush()
                    else:
                        text = _flat(item).strip()
                        if text:
                            if not items:
                                chunk_start = (start if start is not None else 1) + item_index if tag == "ol" else None
                            items.append(text)
                    item_index += 1
                flush_list()
                if list(_descendants(child, "ul")) or list(_descendants(child, "ol")):
                    issues.append("nested_list_flattened")
            elif tag == "table":
                flush()
                for caption in _table_descendants(child, "caption"):
                    text = _flat(caption).strip()
                    if text:
                        nodes.append({"type": "heading", "text": text, "level": 3})
                rows = []
                for row in _table_descendants(child, "tr"):
                    cells = []
                    for cell in row["children"]:
                        if not isinstance(cell, dict) or cell["tag"] not in {"td", "th"}:
                            continue
                        cell_attrs = cell["attrs"]
                        if any(key in cell_attrs and _positive(cell_attrs[key], 100) is None for key in ("rowspan", "colspan")):
                            issues.append("table_span_unsupported")
                        cells.append({"text": _flat(cell).strip(), "header": cell["tag"] == "th",
                                      "rowspan": _positive(cell_attrs.get("rowspan"), 100) or 1,
                                      "colspan": _positive(cell_attrs.get("colspan"), 100) or 1})
                    if cells:
                        rows.append(cells)
                if any(_table_descendants(child, "img")):
                    issues.append("table_images_detached")
                if rows:
                    nodes.append({"type": "table", "rows": rows})
                else:
                    issues.append("table_rows_missing")
                # The v1 cell contract is text-only. Keep images in source order
                # immediately beside their table and explicitly mark the layout
                # as partial instead of silently dropping those source assets.
                for image in _table_descendants(child, "img"):
                    walk([image])
                nested_tables = list(_descendants(child, "table"))
                if nested_tables:
                    issues.append("nested_tables_detached")
                    for nested_table in nested_tables:
                        walk([nested_table])
            elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                flush()
                text = _flat(child).strip()
                if text:
                    nodes.append({"type": "heading", "text": text, "level": int(tag[1])})
                if any(_descendants(child, "img")):
                    issues.append("heading_images_detached")
                    for image in _descendants(child, "img"):
                        walk([image])
            else:
                boundary = tag in {"p", "div", "section", "article", "figure", "figcaption", "hr", "li"}
                if boundary:
                    flush()
                walk(child["children"])
                if boundary:
                    flush()
    walk(parser.root["children"])
    flush()
    return nodes, sorted(set(issues))


def _structure_only(value):
    """Unknown source objects retain field names/types, never field values."""
    def kind(item):
        return "null" if item is None else "boolean" if type(item) is bool else "number" if type(item) in {int, float} else "string" if isinstance(item, str) else "array" if isinstance(item, list) else "object" if isinstance(item, dict) else "unsupported"
    if isinstance(value, dict):
        fields = {_label(key): kind(item) for key, item in list(value.items())[:50]}
        return {"unsupported_shape": {"kind": "object", "fields": fields, "field_count": len(value)}}
    if isinstance(value, list):
        return {"unsupported_shape": {"kind": "array", "item_types": sorted({kind(item) for item in value[:50]}), "item_count": len(value)}}
    return {"unsupported_shape": {"kind": kind(value)}}


def _safe_source_html(value):
    parser = _Tree()
    parser.feed(_text(value))
    parser.close()
    allowed_tags = {"p", "div", "span", "section", "article", "figure", "figcaption", "blockquote", "pre", "code",
                    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "table", "thead", "tbody", "tfoot",
                    "tr", "th", "td", "caption", "colgroup", "col", "br", "hr", "img", "strong", "b", "em", "i",
                    "u", "s", "small", "sup", "sub", "dl", "dt", "dd"}
    def render(node):
        if isinstance(node, str):
            return escape(node, quote=False)
        tag, attrs = node["tag"], node["attrs"]
        children = "".join(render(child) for child in node["children"])
        if tag not in allowed_tags:
            return children
        safe = {}
        if tag == "img":
            source = valid_image_url(attrs.get("src"))
            if source is None:
                return ""
            safe["src"] = source
            if "alt" in attrs:
                safe["alt"] = attrs["alt"][:4000]
            for key in ("width", "height"):
                number = _positive(attrs.get(key))
                if number is not None:
                    safe[key] = str(number)
        elif tag == "ol" and (start := _list_start(attrs.get("start"))) is not None:
            safe["start"] = str(start)
        elif tag in {"th", "td"}:
            for key in ("rowspan", "colspan"):
                number = _positive(attrs.get(key), 100)
                if number is not None:
                    safe[key] = str(number)
        attributes = "".join(f' {key}="{escape(value, quote=True)}"' for key, value in safe.items())
        opening = f"<{tag}{attributes}>"
        return opening if tag in _VOID else opening + children + f"</{tag}>"
    return "".join(render(child) for child in parser.root["children"])


def sanitize_content_source(info) -> dict | None:
    """Bounded merchant evidence, not a raw HTML/router/body persistence path."""
    if not isinstance(info, dict):
        return None
    try:
        source = {key: info[key] for key in ("specifications", "desc_blocks") if key in info}
        if len(json.dumps(source, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_CONTENT_BYTES:
            return None
        result = {}
        for field, maximum in (("specifications", 200), ("desc_blocks", MAX_BLOCKS)):
            if field not in source:
                continue
            value = source[field]
            if value is None:
                result[field] = None
                continue
            if not isinstance(value, list):
                result[field] = _structure_only(value)
                continue
            if len(value) > maximum:
                return None
            rows = []
            for row in value:
                if not isinstance(row, dict):
                    rows.append(_structure_only(row))
                    continue
                if field == "specifications":
                    projected = {}
                    for key in ("name", "value"):
                        if key in row:
                            projected[key] = _description_text(_html_nodes(row[key])[0]) if isinstance(row[key], str) else _structure_only(row[key]) if row[key] is not None else None
                    rows.append(projected)
                    continue
                projected = {"type": _label(row.get("type"))}
                for key in ("text", "content"):
                    if key not in row:
                        continue
                    item = row[key]
                    if isinstance(item, str):
                        projected[key] = _safe_source_html(item)
                    elif isinstance(item, list):
                        projected[key] = [_safe_source_html(part) if isinstance(part, str) else _structure_only(part) if part is not None else None for part in item[:MAX_BLOCKS]]
                        if len(item) > MAX_BLOCKS:
                            return None
                    else:
                        projected[key] = _structure_only(item) if item is not None else None
                rows.append(projected)
            result[field] = rows
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_CONTENT_BYTES:
            return None
        return result
    except (TypeError, ValueError, RecursionError, OverflowError):
        return None


def _validate_nodes(nodes):
    if not isinstance(nodes, list) or len(nodes) > MAX_BLOCKS * 4:
        raise ValueError("商品介绍节点数量无效。")
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("商品介绍节点无效。")
        kind = node.get("type")
        if not isinstance(kind, str):
            raise ValueError("商品介绍节点类型无效。")
        keys = set(node)
        if kind in {"paragraph", "heading"}:
            if keys != ({"type", "text", "level"} if kind == "heading" else {"type", "text"}):
                raise ValueError("商品介绍文字节点含未知字段。")
            _text(node["text"])
            if kind == "heading" and (type(node["level"]) is not int or not 1 <= node["level"] <= 6):
                raise ValueError("商品介绍标题级别无效。")
        elif kind == "list":
            if not {"type", "ordered", "items"} <= keys or keys - {"type", "ordered", "items", "start"} or type(node["ordered"]) is not bool or not isinstance(node["items"], list) or not 1 <= len(node["items"]) <= MAX_BLOCKS:
                raise ValueError("商品介绍列表无效。")
            if "start" in node and (not node["ordered"] or type(node["start"]) is not int or not -1_000_000 <= node["start"] <= 1_000_000):
                raise ValueError("商品介绍列表起始序号无效。")
            for item in node["items"]:
                _text(item)
        elif kind == "table":
            if keys != {"type", "rows"} or not isinstance(node["rows"], list) or not 1 <= len(node["rows"]) <= 200:
                raise ValueError("商品介绍表格无效。")
            for row in node["rows"]:
                if not isinstance(row, list) or not 1 <= len(row) <= 100:
                    raise ValueError("商品介绍表格行无效。")
                for cell in row:
                    if not isinstance(cell, dict) or set(cell) != {"text", "header", "rowspan", "colspan"} or type(cell["header"]) is not bool:
                        raise ValueError("商品介绍表格单元格无效。")
                    _text(cell["text"])
                    if any(type(cell[key]) is not int or not 1 <= cell[key] <= 100 for key in ("rowspan", "colspan")):
                        raise ValueError("商品介绍表格跨度无效。")
        elif kind == "image":
            if keys != {"type", "url", "alt", "role", "width", "height"} or valid_image_url(node["url"]) is None or node["role"] != "description":
                raise ValueError("商品介绍图片无效。")
            _text(node["alt"], 4000)
            if any(node[key] is not None and (type(node[key]) is not int or not 1 <= node[key] <= 100_000) for key in ("width", "height")):
                raise ValueError("商品介绍图片尺寸无效。")
        else:
            raise ValueError("商品介绍节点类型未支持。")


def _description_text(nodes):
    lines = []
    for node in nodes:
        if node["type"] in {"paragraph", "heading"}:
            lines.append(node["text"])
        elif node["type"] == "list":
            lines.extend(node["items"])
        elif node["type"] == "table":
            lines.extend("\t".join(cell["text"] for cell in row) for row in node["rows"])
    return "\n".join(lines) or None


def _completeness(states, issues=()):
    states = set(states)
    return "partial" if issues else "missing" if states == {"missing"} else "partial" if "partial" in states or "missing" in states else "empty" if states == {"empty"} else "complete"


@dataclass(frozen=True)
class ProductContentProjection:
    source_version: str
    sections: dict
    description_text: str | None
    completeness: str
    unsupported_blocks: tuple[str, ...]
    source_shape: dict

    @classmethod
    def from_record(cls, record: dict):
        if not isinstance(record, dict) or set(record) != set(cls.__dataclass_fields__) or record.get("source_version") != CONTENT_SOURCE_VERSION or not isinstance(record.get("completeness"), str) or record["completeness"] not in COMPLETENESS:
            raise ValueError("商品介绍投影版本或结构无效。")
        sections, shape, issues = record["sections"], record["source_shape"], record["unsupported_blocks"]
        if not isinstance(sections, dict) or set(sections) != {"specifications", "description"} or not isinstance(sections["specifications"], list) or len(sections["specifications"]) > 200:
            raise ValueError("商品介绍章节无效。")
        for row in sections["specifications"]:
            if not isinstance(row, dict) or set(row) != {"name", "value", "source"} or row["source"] != "specifications":
                raise ValueError("商品介绍参数来源无效。")
            _text(row["name"], 255)
            _text(row["value"], 4000)
            if not row["name"].strip() or not row["value"].strip():
                raise ValueError("商品介绍参数不能为空。")
        _validate_nodes(sections["description"])
        if record["description_text"] is not None:
            _text(record["description_text"])
        if record["description_text"] != _description_text(sections["description"]):
            raise ValueError("商品介绍文字与有序节点不一致。")
        if not isinstance(issues, (tuple, list)) or len(issues) > 1000 or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_:.-]{1,128}", value) for value in issues):
            raise ValueError("商品介绍缺失标记无效。")
        if not isinstance(shape, dict) or set(shape) != _SHAPE_KEYS:
            raise ValueError("商品介绍来源结构无效。")
        for key in ("specifications", "desc_blocks"):
            entry = shape[key]
            if not isinstance(entry, dict) or set(entry) != {"present", "kind", "count", "types"} or type(entry["present"]) is not bool or not isinstance(entry["kind"], str) or entry["kind"] not in {"missing", "null", "list", "invalid"} or type(entry["count"]) is not int or not 0 <= entry["count"] <= 100_000:
                raise ValueError("商品介绍来源形状无效。")
            if not isinstance(entry["types"], dict) or len(entry["types"]) > MAX_BLOCKS or any(_label(name) != name or type(count) is not int or not 0 <= count <= MAX_BLOCKS for name, count in entry["types"].items()):
                raise ValueError("商品介绍来源类型计数无效。")
            if (not entry["present"] and entry["kind"] != "missing") or (entry["present"] and entry["kind"] == "missing") or (entry["kind"] != "list" and (entry["count"] or entry["types"])):
                raise ValueError("商品介绍字段存在性与来源结构不一致。")
            if key == "desc_blocks" and entry["kind"] == "list" and sum(entry["types"].values()) != min(entry["count"], MAX_BLOCKS):
                raise ValueError("商品介绍节点计数与来源结构不一致。")
        if not isinstance(shape["sections"], dict) or set(shape["sections"]) != {"specifications", "description"} or any(not isinstance(value, str) or value not in COMPLETENESS for value in shape["sections"].values()):
            raise ValueError("商品介绍章节覆盖状态无效。")
        if not isinstance(shape["issues"], list) or shape["issues"] != list(issues):
            raise ValueError("商品介绍来源缺失标记不一致。")
        for section, source in (("specifications", "specifications"), ("description", "desc_blocks")):
            state = shape["sections"][section]
            if (state in {"missing", "empty"} and sections[section]) or (state == "complete" and not sections[section]) or (state == "missing" and shape[source]["kind"] not in {"missing", "null"}):
                raise ValueError("商品介绍章节内容与覆盖状态不一致。")
        if record["completeness"] != _completeness(shape["sections"].values(), issues):
            raise ValueError("商品介绍覆盖状态与来源证据不一致。")
        serialized = json.dumps(record, ensure_ascii=False, allow_nan=False)
        if len(serialized.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError("商品介绍投影过大。")
        copied = json.loads(serialized)
        copied["unsupported_blocks"] = tuple(copied["unsupported_blocks"])
        return cls(**copied)


def _shape(info, field):
    value = info.get(field)
    return {"present": field in info, "kind": "missing" if field not in info else "null" if value is None else "list" if isinstance(value, list) else "invalid",
            "count": min(len(value), 100_000) if isinstance(value, list) else 0,
            "types": dict(Counter(_label(item.get("type")) if isinstance(item, dict) else "invalid" for item in value[:MAX_BLOCKS])) if field == "desc_blocks" and isinstance(value, list) else {}}


def parse_product_content(info: dict) -> ProductContentProjection:
    """Project merchant content; malformed optional sections never invent data."""
    if not isinstance(info, dict):
        raise ValueError("商品介绍必须来自已校验的 product_info。")
    sections = {"specifications": [], "description": []}
    shape = {"specifications": _shape(info, "specifications"), "desc_blocks": _shape(info, "desc_blocks"), "sections": {}, "issues": []}
    try:
        too_large = len(json.dumps({key: info.get(key) for key in ("specifications", "desc_blocks")}, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_CONTENT_BYTES
    except (TypeError, ValueError, RecursionError):
        too_large = True
    if too_large:
        shape["sections"] = {"specifications": "partial", "description": "partial"}
        shape["issues"] = ["source:exceeds_supported_limits"]
        return ProductContentProjection.from_record({"source_version": CONTENT_SOURCE_VERSION,
            "sections": sections, "description_text": None, "completeness": "partial",
            "unsupported_blocks": shape["issues"], "source_shape": shape})
    issues = []
    for field, target, limit in (("specifications", "specifications", 200), ("desc_blocks", "description", MAX_BLOCKS)):
        value = info.get(field)
        before = len(issues)
        if field not in info or value is None:
            shape["sections"][target] = "missing"
            continue
        if not isinstance(value, list) or len(value) > limit:
            issues.append(field + ":invalid_structure")
            shape["sections"][target] = "partial"
            continue
        for position, row in enumerate(value):
            prefix = f"{field}:{position}:"
            if not isinstance(row, dict):
                issues.append(prefix + "invalid_row")
                continue
            if field == "specifications":
                try:
                    name, value_text = _text(row.get("name"), 255).strip(), _text(row.get("value"), 4000).strip()
                    if not name or not value_text:
                        raise ValueError
                    sections[target].append({"name": name, "value": value_text, "source": "specifications"})
                except ValueError:
                    issues.append(prefix + "invalid_parameter")
                continue
            kind = _label(row.get("type"))
            if kind not in {"text", "ul", "ol", "image", "table"}:
                issues.append(prefix + "unsupported_" + kind)
                continue
            content = row.get("text") if kind == "text" and isinstance(row.get("text"), str) else row.get("content")
            try:
                if kind in {"ul", "ol"} and isinstance(content, list):
                    if len(content) > MAX_BLOCKS:
                        raise ValueError
                    items = []
                    def flush_items():
                        if items:
                            sections[target].append({"type": "list", "ordered": kind == "ol", "items": list(items)})
                            items.clear()
                    for item in content:
                        try:
                            parsed, warnings = _html_nodes(item)
                        except (ValueError, RecursionError):
                            issues.append(prefix + "invalid_list_item")
                            continue
                        issues.extend(prefix + warning for warning in warnings)
                        if any(node["type"] not in {"paragraph", "heading"} for node in parsed):
                            issues.append(prefix + "complex_list_item")
                            flush_items()
                            sections[target].extend(parsed)
                        else:
                            text = "\n".join(node["text"] for node in parsed)
                            if text:
                                items.append(text)
                    flush_items()
                elif isinstance(content, str):
                    parsed, warnings = _html_nodes(content)
                    if kind in {"image", "table"} and not any(node["type"] == kind for node in parsed):
                        issues.append(prefix + "source_structure_unverified")
                    sections[target].extend(parsed)
                    issues.extend(prefix + warning for warning in warnings)
                else:
                    issues.append(prefix + "unsupported_content_" + kind)
            except (ValueError, RecursionError):
                issues.append(prefix + "invalid_content")
        shape["sections"][target] = "partial" if len(issues) != before else "complete" if sections[target] else "empty"
    completeness = _completeness(shape["sections"].values(), issues)
    issues = sorted(set(issues))
    shape["issues"] = issues
    record = {"source_version": CONTENT_SOURCE_VERSION, "sections": sections,
              "description_text": _description_text(sections["description"]), "completeness": completeness,
              "unsupported_blocks": issues, "source_shape": shape}
    try:
        return ProductContentProjection.from_record(record)
    except ValueError:
        # Optional rich content must not turn a valid price/SKU observation into
        # a failure. Preserve a bounded coverage record instead of truncating
        # nodes while claiming that the original content was captured fully.
        shape["sections"] = {"specifications": "partial", "description": "partial"}
        shape["issues"] = ["projection:exceeds_supported_limits"]
        return ProductContentProjection.from_record({
            "source_version": CONTENT_SOURCE_VERSION,
            "sections": {"specifications": [], "description": []},
            "description_text": None, "completeness": "partial",
            "unsupported_blocks": shape["issues"], "source_shape": shape,
        })
