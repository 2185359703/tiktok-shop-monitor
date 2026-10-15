"""Backfill genuine prior observations from successful local detail evidence."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.crawlers.product_detail import DetailSnapshot
from app.models import CrawlAttempt, CrawlTask, Product, ResourceType, TaskStatus, TaskType
from app.services.snapshot_service import persist_snapshot
from app.settings import Settings
from app.storage.database import PROJECT_ROOT, make_engine, make_session_factory, session_scope


def main():
    parser = argparse.ArgumentParser(description="从已成功的真实详情证据补建历史基线，默认只展示计划")
    parser.add_argument("--product-id", action="append", default=[])
    parser.add_argument("--max-products", type=int, default=3)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.max_products <= 100: raise ValueError("基线补录预算必须在 1-100。")
    settings = Settings.load()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    try:
        with factory() as db:
            query = select(Product).where(Product.platform == settings.platform, Product.region == settings.region,
                                           Product.last_success_at.is_not(None))
            if args.product_id: query = query.where(Product.platform_product_id.in_(args.product_id))
            products = db.scalars(query.order_by(Product.id).limit(args.max_products)).all()
            plan = []
            for product in products:
                task = db.scalar(select(CrawlTask).where(CrawlTask.platform == product.platform, CrawlTask.region == product.region,
                    CrawlTask.task_type == TaskType.PRODUCT_DETAIL, CrawlTask.resource_type == ResourceType.PRODUCT,
                    CrawlTask.resource_id == str(product.id), CrawlTask.status == TaskStatus.SUCCEEDED)
                    .order_by(CrawlTask.finished_at, CrawlTask.id).limit(1))
                if task: plan.append({"product_id": str(product.id), "platform_product_id": product.platform_product_id, "task_id": task.task_id})
        if not args.execute:
            print(json.dumps({"dry_run": True, "baseline_candidates": plan}, ensure_ascii=False, indent=2))
            return 0
        report = []
        for item in plan:
            with session_scope(factory) as db:
                task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == item["task_id"], CrawlTask.status == TaskStatus.SUCCEEDED).with_for_update())
                attempt = db.scalar(select(CrawlAttempt).where(CrawlAttempt.task_id == task.id, CrawlAttempt.succeeded.is_(True))
                                    .order_by(CrawlAttempt.attempt_no.desc()).limit(1))
                if attempt is None or not attempt.evidence_path: raise ValueError("成功详情缺少原始证据。")
                path = Path(attempt.evidence_path).resolve()
                if not path.is_relative_to((PROJECT_ROOT / "data/samples/runtime").resolve()) or path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError("详情证据不在项目运行证据目录内。")
                response = DetailSnapshot.from_evidence(json.loads(path.read_text(encoding="utf-8")))
                if response.response_hash != attempt.response_hash or response.captured_at > attempt.finished_at:
                    raise ValueError("证据响应摘要或观察时间与成功尝试不一致。")
                summary = persist_snapshot(db, task, response, update_current=False)
                report.append({**item, **summary, "captured_at": response.captured_at.isoformat(), "evidence_path": str(path)})
        output = args.output or PROJECT_ROOT / f"data/samples/snapshots/backfill-{uuid4().hex[:12]}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"snapshots_written": sum(row["snapshots_written"] for row in report),
                          "variant_snapshots_written": sum(row["variant_snapshots_written"] for row in report), "report": str(output)}, ensure_ascii=False))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "真实基线补录未完成；检查证据与已有快照顺序。"}, ensure_ascii=False))
        raise SystemExit(1)
