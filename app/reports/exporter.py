"""Portable reports named by their content; interrupted writes leave no partial file."""

import html
import json
import os
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.storage.database import PROJECT_ROOT


TEMPLATE = Path(__file__).parent / "templates/monitor.html"


def markdown_cell(value):
    return html.escape(str(value if value is not None else "—")).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render_markdown(report):
    summary = report["summary"]
    lines = ["# TikTok Shop 美国服装监测报告", "", f"观察截止时间：{report['as_of']}；价格上下文：{report['pricing_context']} / {report['currency']}。", "",
             f"当前有公开入口的店铺 {summary['shop_entries']} 家，已采样 {summary['sampled_shops']} 家；商品 {summary['products']} 个，其中有历史详情证据 {summary['historical_detail_products']} 个。",
             f"最新有效观察包含 {summary['observed_skus']} 个 SKU；状态明确的 {summary['known_stock_skus']} 个，缺货 {summary['stockout_skus']} 个，缺货率 {summary['sku_stockout_pct'] or '—'}%。观察区间事件 {summary['events_in_window']} 条。", "",
             "## 样本价格与店铺", "", "| 店铺 | 已发现商品 | 历史详情商品 | 已见 SKU | 公开起价中位数 | SKU 缺货率 |", "| --- | --- | --- | --- | --- | --- |"]
    for shop in report["shops"]:
        if not shop["sampled_products"]: continue
        row = [shop["name"], shop["sampled_products"], shop["historical_detail_products"], shop["sku_count"],
               shop["starting_price_stats"]["median"], (shop["sku_stockout_pct"] + "%") if shop["sku_stockout_pct"] is not None else None]
        lines.append("| " + " | ".join(markdown_cell(value) for value in row) + " |")
    lines.extend(["", "## 变化事件与证据", "", "| 事件 | 店铺 | 商品 ID | SKU 内部 ID | 前快照 | 后快照 |", "| --- | --- | --- | --- | --- | --- |"])
    for event in report["events"]:
        row = [event["event_type"], event["shop_name"], event["platform_product_id"], event["variant_id"], event["previous_snapshot_id"], event["snapshot_id"]]
        lines.append("| " + " | ".join(markdown_cell(value) for value in row) + " |")
    if not report["events"]: lines.append("| 本区间未记录事件 | — | — | — | — | — |")
    lines.extend(["", "## 使用口径", ""] + ["- " + markdown_cell(note) for note in report["notes"]])
    return "\n".join(lines) + "\n"


def json_chunks(report, *, pretty=False, html_safe=False):
    options = {"ensure_ascii": False, "allow_nan": False}
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    for chunk in json.JSONEncoder(**options).iterencode(report):
        # Escape each token before encoding so a single emoji does not make a
        # complete multi-megabyte JSON/HTML string use a four-byte text width.
        yield chunk.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") if html_safe else chunk


def html_chunks(report):
    parts = TEMPLATE.read_text(encoding="utf-8").split("__REPORT_JSON__")
    if len(parts) != 2:
        raise ValueError("报告模板必须含且仅含一个 JSON 插槽。")
    yield parts[0]
    yield from json_chunks(report, html_safe=True)
    yield parts[1]


def html_response_bytes(json_body):
    parts = TEMPLATE.read_text(encoding="utf-8").split("__REPORT_JSON__")
    if len(parts) != 2:
        raise ValueError("报告模板必须含且仅含一个 JSON 插槽。")
    with BytesIO() as stream:
        stream.write(parts[0].encode("utf-8"))
        for offset in range(0, len(json_body), 65536):
            stream.write(json_body[offset:offset + 65536].replace(b"<", b"\\u003c")
                         .replace(b">", b"\\u003e").replace(b"&", b"\\u0026"))
        stream.write(parts[1].encode("utf-8"))
        return stream.getvalue()


def encoded_chunks(chunks):
    for chunk in chunks:
        yield chunk.encode("utf-8")


def response_bytes(chunks):
    with BytesIO() as stream:
        for chunk in encoded_chunks(chunks):
            stream.write(chunk)
        return stream.getvalue()


def render_html(report):
    # Small callers may need a string; large API/export paths consume chunks.
    return "".join(html_chunks(report))


def file_hash(path):
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_report_file(directory, chunks):
    path = None
    digest = sha256()
    try:
        with NamedTemporaryFile("wb", dir=directory, prefix=".report-", suffix=".tmp", delete=False) as stream:
            path = Path(stream.name)
            for chunk in encoded_chunks(chunks):
                stream.write(chunk)
                digest.update(chunk)
        return path, digest.hexdigest()
    except BaseException:
        if path is not None and path.exists(): path.unlink()
        raise


def export_report(report, directory=None):
    directory = Path(directory or PROJECT_ROOT / "data/exports/reports")
    directory.mkdir(parents=True, exist_ok=True)
    def pretty_json():
        yield from json_chunks(report, pretty=True)
        yield "\n"
    # Compute the content-addressed name while writing JSON. HTML is streamed
    # afterwards, never held with three whole-document string copies in RAM.
    json_temporary, digest = stage_report_file(directory, pretty_json())
    prefix = f"us-clothing-{report['as_of'][:10]}-{digest[:16]}"
    paths = {}
    try:
        for extension in ("json", "md", "html"):
            temporary = None
            try:
                if extension == "json":
                    temporary, content_hash = json_temporary, digest
                else:
                    chunks = (render_markdown(report),) if extension == "md" else html_chunks(report)
                    temporary, content_hash = stage_report_file(directory, chunks)
                target = directory / f"{prefix}.{extension}"
                if target.is_file():
                    if file_hash(target) != content_hash:
                        raise ValueError("报表文件摘要与已存在内容冲突。")
                else:
                    os.replace(temporary, target)
                paths[extension] = str(target.resolve())
            finally:
                if temporary is not None and temporary.exists(): temporary.unlink()
    finally:
        if json_temporary.exists(): json_temporary.unlink()
    return {"report_hash": digest, "files": paths}
