"""Read indexed public product pages to discover their actual shop links."""

import json

from sqlalchemy import select

from app.crawlers.public_shop import PublicPageError, PublicShopClient, public_url, shop_id_from_url
from app.models import SeedShopObservation, Shop, ShopSeedLink
from app.services.seed_importer import read_seed_csv
from app.services.shop_discovery import normalized_name
from app.storage.database import PROJECT_ROOT, make_engine


def main() -> int:
    candidate_file = PROJECT_ROOT / "data/seed/shop_link_candidates.json"
    source_file = PROJECT_ROOT / "data/seed/shop_link_source_pages.json"
    document = json.loads(candidate_file.read_text(encoding="utf-8"))
    sources = json.loads(source_file.read_text(encoding="utf-8"))["pages"]
    seeds = read_seed_csv(PROJECT_ROOT / "data/seed/archive/shops_us_clothing_names_20260925.csv")
    names = {normalized_name(str(row.normalized["seed_value"])) for row in seeds.rows}
    by_id = {}
    engine = make_engine()
    try:
        with engine.connect() as connection:
            for name, identifier in connection.execute(select(SeedShopObservation.seed_value, Shop.platform_shop_id)
                .join(ShopSeedLink, ShopSeedLink.seed_id == SeedShopObservation.id)
                .join(Shop, Shop.id == ShopSeedLink.shop_id)):
                by_id[identifier] = normalized_name(name)
    finally:
        engine.dispose()
    covered = set()
    known_urls = set()
    for proposal in document["candidates"]:
        identifier = shop_id_from_url(proposal["url"])
        covered.add(normalized_name(proposal["name"]))
        if identifier in by_id:
            covered.add(by_id[identifier])
        known_urls.add(public_url(proposal["url"]))
    fetched, discovered = 0, 0
    errors = []
    with PublicShopClient() as client:
        for source in sources:
            requested = {normalized_name(name) for name in source["queried_names"]}
            if not (requested & names) - covered:
                continue
            url = public_url(source["url"])
            print(f"核对商品来源：{url}", flush=True)
            try:
                snapshot = client.fetch(url)
                fetched += 1
                for shop in snapshot.candidates:
                    aliases = {normalized_name(alias) for alias in shop.observed_names}
                    matching = aliases & names
                    if shop.platform_shop_id in by_id:
                        matching.add(by_id[shop.platform_shop_id])
                    if not matching or not shop.canonical_url or shop_id_from_url(shop.canonical_url) != shop.platform_shop_id:
                        continue
                    link = public_url(shop.canonical_url)
                    covered.update(matching)
                    if link in known_urls:
                        continue
                    evidence = snapshot.save_evidence(PROJECT_ROOT / "data/samples/seed-links/product-sources")
                    document["candidates"].append({"name": shop.canonical_name, "url": link,
                        "discovery_source": snapshot.final_url,
                        "discovery_evidence": evidence.relative_to(PROJECT_ROOT).as_posix()})
                    known_urls.add(link)
                    discovered += 1
                    print(f"发现真实店铺入口：{shop.canonical_name} {link}", flush=True)
                candidate_file.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except PublicPageError as exc:
                errors.append({"source_url": url, "code": exc.code, "http_status": exc.status})
                print(f"来源读取失败：{exc.code}，HTTP {exc.status or '无响应'}。", flush=True)
                if exc.code == "access_limited":
                    break
    result = {"pages_fetched": fetched, "new_store_urls": discovered, "errors": errors,
              "names_without_candidates": sorted(names - covered)}
    path = PROJECT_ROOT / "data/samples/seed-links/product-source-results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
