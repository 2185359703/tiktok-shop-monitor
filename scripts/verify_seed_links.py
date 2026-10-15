"""Check observed storefront proposals; emit evidence without editing the seed CSV."""

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.crawlers.public_shop import PublicPageError, PublicPageSnapshot, PublicShopClient, ShopCandidate, public_url, shop_id_from_url
from app.models import SeedShopObservation, Shop, ShopSeedLink
from app.services.seed_importer import normalize_seed, read_seed_csv
from app.services.seed_link_verification import sync_verified_store_link, verify_store_link
from app.services.shop_discovery import normalized_name
from app.storage.database import PROJECT_ROOT, make_engine, make_session_factory, session_scope


def observation_anchors(connection, batch):
    """Map enriched input rows back to immutable DB observations by scoped identity."""
    records = connection.execute(select(
        SeedShopObservation.row_hash, SeedShopObservation.seed_value, SeedShopObservation.category,
        SeedShopObservation.region, SeedShopObservation.raw_record, Shop.platform_shop_id,
    ).outerjoin(ShopSeedLink, ShopSeedLink.seed_id == SeedShopObservation.id)
     .outerjoin(Shop, Shop.id == ShopSeedLink.shop_id)
     .where(SeedShopObservation.platform == "tiktok_shop",
            SeedShopObservation.seed_value.in_([str(row.normalized["seed_value"]) for row in batch.rows]))
     .order_by(SeedShopObservation.id)).mappings().all()
    by_key = defaultdict(list)
    for record in records:
        by_key[(record["region"], record["category"], normalized_name(record["seed_value"]))].append(record)
    anchors, identifiers = {}, {}
    for row in batch.rows:
        key = (row.normalized["region"], row.normalized["category"], normalized_name(str(row.normalized["seed_value"])))
        matching = by_key.get(key, [])
        exact = next((record for record in matching if record["row_hash"] == row.row_hash), None)
        linked = [record for record in matching if record["platform_shop_id"] is not None]
        ids = {record["platform_shop_id"] for record in linked}
        if len(ids) > 1 and exact is None:
            raise ValueError("同一市场和分组的名称对应多个已确认店铺ID，请先人工确认。")
        chosen = exact or (linked[0] if linked else matching[0] if matching else None)
        anchor = row
        if chosen and chosen["raw_record"]:
            anchor = normalize_seed(chosen["raw_record"], 1)
            if anchor.row_hash != chosen["row_hash"]:
                raise ValueError("数据库原始观察与摘要不一致，不自动改写。")
        anchors[row.row_hash] = anchor
        if chosen and chosen["platform_shop_id"]:
            identifiers[row.row_hash] = chosen["platform_shop_id"]
    return anchors, identifiers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=PROJECT_ROOT / "data/seed/shops_us_clothing.csv")
    parser.add_argument("--candidates", type=Path, default=PROJECT_ROOT / "data/seed/shop_link_candidates.json")
    parser.add_argument("--report", type=Path, default=PROJECT_ROOT / "data/samples/seed-links/verification.json")
    parser.add_argument("--max-stores", type=int, default=120)
    parser.add_argument("--sync-database", action="store_true", help="Apply verified entrances to existing original observations.")
    args = parser.parse_args(argv)
    batch = read_seed_csv(args.file)
    candidates = json.loads(args.candidates.read_text(encoding="utf-8"))["candidates"]
    known_ids, anchors = {}, {}
    engine = make_engine()
    try:
        with engine.connect() as connection:
            anchors, known_ids = observation_anchors(connection, batch)
    finally:
        engine.dispose()
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "input_rows": batch.input_rows,
              "verified": [], "unresolved": [], "source_errors": [], "stores_fetched": 0}
    # Same-day successful evidence may be reused when continuing this exact file.
    previous = {}
    previous_by_name = {}
    if args.report.is_file():
        old = json.loads(args.report.read_text(encoding="utf-8"))
        for item in old.get("verified", []):
            age = datetime.now(timezone.utc) - datetime.fromisoformat(item["verified_at"])
            if 0 <= age.total_seconds() < 86400:
                previous[item["original_row_hash"]] = item
                previous_by_name[(item["category"], normalized_name(item["seed_value"]))] = item
    cached = {}
    limited = False

    def persist():
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with PublicShopClient() as client:
        for row in batch.rows:
            original_hash = anchors[row.row_hash].row_hash
            known_id = known_ids.get(row.row_hash)
            options = [public_url(str(row.normalized["shop_url"]))] if row.normalized["shop_url"] else []
            provided_id = shop_id_from_url(options[0]) if options else None
            if known_id and provided_id and provided_id != known_id:
                report["unresolved"].append({"original_row_hash": original_hash,
                    "seed_value": row.normalized["seed_value"], "category": row.normalized["category"],
                    "reason": "identity_conflict", "candidate_urls": options, "errors": []})
                persist()
                continue
            for proposal in candidates:
                url = public_url(proposal["url"])
                if normalized_name(proposal["name"]) == normalized_name(str(row.normalized["seed_value"])) or (
                    known_id is not None and shop_id_from_url(url) == known_id
                ):
                    if url not in options:
                        options.append(url)
            old = previous.get(original_hash) or previous_by_name.get((
                str(row.normalized["category"]), normalized_name(str(row.normalized["seed_value"]))
            ))
            if old and old["proposed_url"] in options and (known_id is None or old["platform_shop_id"] == known_id):
                report["verified"].append({**old, "original_row_hash": original_hash, "input_row_hash": row.row_hash})
                continue
            successes = {}
            errors = []
            if not limited:
                for url in options:
                    try:
                        if url not in cached:
                            if report["stores_fetched"] >= args.max_stores:
                                limited = True
                                break
                            print(f"核对店铺：{row.normalized['seed_value']} {url}", flush=True)
                            snapshot = client.fetch(url)
                            report["stores_fetched"] += 1
                            evidence = snapshot.save_evidence(args.report.parent / "pages")
                            try:
                                evidence_path = evidence.relative_to(PROJECT_ROOT).as_posix()
                            except ValueError:
                                evidence_path = str(evidence.resolve())
                            cached[url] = (snapshot, evidence_path)
                        snapshot, evidence_path = cached[url]
                        checked = verify_store_link(row, snapshot, expected_id=known_id)
                        successes[checked.shop.platform_shop_id] = {
                            "original_row_hash": original_hash, "seed_value": row.normalized["seed_value"],
                            "input_row_hash": row.row_hash,
                            "category": row.normalized["category"], "shop_url": checked.shop_url,
                            "platform_shop_id": checked.shop.platform_shop_id,
                            "canonical_name": checked.shop.canonical_name,
                            "verified_at": snapshot.captured_at.isoformat(), "evidence_path": evidence_path,
                            "matched_by": checked.matched_by, "proposed_url": url,
                        }
                        # Existing identity anchors disambiguate alternate URLs.
                        if known_id is not None:
                            break
                    except PublicPageError as exc:
                        errors.append({"url": url, "code": exc.code, "http_status": exc.status})
                        if exc.code == "access_limited":
                            report["source_errors"].append(errors[-1])
                            limited = True
                            break
            if len(successes) == 1:
                checked = next(iter(successes.values()))
                report["verified"].append(checked)
                print(f"已确认：{checked['seed_value']} → {checked['platform_shop_id']}", flush=True)
            else:
                report["unresolved"].append({
                    "original_row_hash": original_hash, "seed_value": row.normalized["seed_value"],
                    "category": row.normalized["category"], "candidate_urls": options,
                    "reason": "ambiguous_identity" if len(successes) > 1 else (
                        "access_or_budget_limit" if limited else "no_verified_storefront"
                    ), "errors": errors,
                })
            persist()
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    if args.sync_database:
        engine = make_engine()
        outcomes = {}
        rows_by_hash = {anchor.row_hash: anchor for anchor in anchors.values()}
        try:
            factory = make_session_factory(engine)
            for item in report["verified"]:
                evidence = json.loads((PROJECT_ROOT / item["evidence_path"]).read_text(encoding="utf-8"))
                snapshot = PublicPageSnapshot(
                    evidence["source_url"], evidence["final_url"], evidence["status"],
                    evidence["response_bytes"], evidence["response_hash"], evidence["latency_ms"],
                    evidence["network_attempts"], datetime.fromisoformat(evidence["started_at"]),
                    datetime.fromisoformat(evidence["captured_at"]), tuple(ShopCandidate(
                        entry["platform_shop_id"], entry["canonical_name"], tuple(entry["observed_names"]),
                        entry["canonical_url"], entry["region"], tuple(entry["evidence_paths"]),
                    ) for entry in evidence["candidates"]),
                )
                with session_scope(factory) as session:
                    outcome = sync_verified_store_link(session, rows_by_hash[item["original_row_hash"]], snapshot,
                                                      item["evidence_path"])
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
        finally:
            engine.dispose()
        report["database_sync"] = outcomes
    persist()
    print(json.dumps({"verified_links": len(report["verified"]), "unresolved": len(report["unresolved"]),
                      "stores_fetched": report["stores_fetched"], "access_limited": limited}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SQLAlchemyError as exc:
        codes = getattr(getattr(exc, "orig", None), "args", ())
        print(f"店铺链接核验失败：{type(exc).__name__}，MySQL code {codes[0] if codes else 'unknown'}。")
        raise SystemExit(1) from None
    except (ValueError, OSError, PublicPageError) as exc:
        print(f"店铺链接核验失败：{exc}")
        raise SystemExit(1) from None
