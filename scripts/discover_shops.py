"""Resolve imported seeds against a bounded set of live public source pages."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import String, and_, cast, select
from sqlalchemy.exc import SQLAlchemyError

from app.crawlers.public_shop import PublicPageError, PublicShopClient, public_url
from app.models import CrawlTask, ResolutionStatus, ResourceType, SeedShopObservation, TaskStatus, TaskType
from app.services.shop_discovery import resolve_seed_from_snapshot
from app.storage.database import PROJECT_ROOT, make_engine, make_session_factory, session_scope


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-sources", type=int, default=6, help="Maximum public source pages to read.")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between HTTP requests, at least 2.")
    parser.add_argument("--evidence-dir", type=Path, default=PROJECT_ROOT / "data/samples/shop-discovery")
    args = parser.parse_args(argv)
    if args.max_sources < 1 or args.interval < 2:
        parser.error("max-sources 必须至少为 1，interval 必须至少为 2 秒。")
    engine = None
    try:
        engine = make_engine()
        factory = make_session_factory(engine)
        sources: dict[str, list[int]] = defaultdict(list)
        unsupported = 0
        with factory() as session:
            rows = session.execute(select(SeedShopObservation.id, SeedShopObservation.source_url).join(
                CrawlTask, and_(CrawlTask.resource_type == ResourceType.SEED,
                    CrawlTask.task_type == TaskType.SHOP_DISCOVERY,
                    CrawlTask.platform == SeedShopObservation.platform,
                    CrawlTask.region == SeedShopObservation.region,
                    CrawlTask.resource_id == cast(SeedShopObservation.id, String)),
            ).where(
                SeedShopObservation.platform == "tiktok_shop", SeedShopObservation.region == "US",
                SeedShopObservation.resolution_status == ResolutionStatus.PENDING,
                CrawlTask.status == TaskStatus.PENDING, CrawlTask.attempt_count < CrawlTask.max_attempts,
            ).distinct().order_by(SeedShopObservation.id)).all()
        for identifier, source_url in rows:
            try:
                sources[public_url(source_url)].append(identifier)
            except PublicPageError:
                unsupported += 1
        outcomes: Counter = Counter()
        report = {"sources_fetched": 0, "source_errors": [], "unsupported_seed_sources": unsupported,
                  "new_product_discovery_tasks": 0, "results": outcomes}
        with PublicShopClient(interval=args.interval) as client:
            for url in sorted(sources)[:args.max_sources]:
                print(f"读取公开来源：{url}", flush=True)
                try:
                    snapshot = client.fetch(url)
                except PublicPageError as exc:
                    report["source_errors"].append({"source_url": url, "code": exc.code, "http_status": exc.status})
                    print(f"来源读取失败：{exc.code}，HTTP {exc.status or '无响应'}；保留种子待解析。", flush=True)
                    if exc.code == "access_limited":
                        break
                    continue
                evidence = snapshot.save_evidence(args.evidence_dir)
                try:
                    evidence_path = evidence.resolve().relative_to(PROJECT_ROOT).as_posix()
                except ValueError:
                    evidence_path = str(evidence.resolve())
                with session_scope(factory) as session:
                    for seed_id in sources[url]:
                        outcome, created = resolve_seed_from_snapshot(session, seed_id, snapshot, evidence_path)
                        outcomes[outcome] += 1
                        report["new_product_discovery_tasks"] += int(created)
                report["sources_fetched"] += 1
                print(f"确认来源有效：{len(snapshot.candidates)} 个去重店铺候选。", flush=True)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["source_errors"] else 0
    except (ValueError, OSError) as exc:
        print(f"店铺来源解析失败：{exc}")
    except SQLAlchemyError as exc:
        codes = getattr(getattr(exc, "orig", None), "args", ())
        print(f"店铺来源解析失败：{type(exc).__name__}，MySQL code {codes[0] if codes else 'unknown'}。")
    finally:
        if engine is not None:
            engine.dispose()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
