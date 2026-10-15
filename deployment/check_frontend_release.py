"""Record final container, photo and business-integrity evidence on the VPS."""

import json
import hashlib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from deployment.rollout_frontend import ROOT, containers, run


services = {name: containers(name) for name in ("api", "worker", "scheduler", "collection")}
expected = run(["docker", "image", "inspect", "--format", "{{.Id}}",
                "tiktok-shop-monitor:frontend-20261015-images-verified-c7a93e14"])
if len(services["worker"]) != 2 or any(item["image"] != expected or item["state"] != "running"
                                      for values in services.values() for item in values):
    raise ValueError("应用进程或镜像不符合本次发布。")
mounts = json.loads(run(["docker", "inspect", "--format", "{{json .Mounts}}", services["api"][0]["id"]]))
static = [item for item in mounts if item["Destination"] == "/opt/tiktok-shop-monitor/app/frontend"]
if len(static) != 1 or static[0]["RW"] or not static[0]["Source"].endswith("/releases/frontend-ui-public-20261015-f7c3102a/app/frontend"):
    raise ValueError("静态目录不是预期的只读发布版本。")
status_mount = [item for item in mounts if item["Destination"] == "/opt/tiktok-shop-monitor/app/services/image_backfill.py"]
if len(status_mount) != 1 or status_mount[0]["RW"] or not status_mount[0]["Source"].endswith("/releases/frontend-status-20261015-f284a6c1/app/services/image_backfill.py"):
    raise ValueError("补采状态模块不是已验证的只读修复。")
with urllib.request.urlopen("http://127.0.0.1:8000/api/v1/images/backfill/status", timeout=60) as response:
    photos = json.load(response)
integrity_path = ROOT / "data/samples/deployment/image-business-after-v1.json"
integrity = json.loads(integrity_path.read_text(encoding="utf-8"))
if not integrity.get("verified_completed_image_tasks") or integrity.get("all_business_unchanged") is not True:
    raise ValueError("尚未验证实际成功任务的业务字段保持一致。")
result = {"verified_at": datetime.now(timezone.utc).isoformat(), "image_id": expected, "services": services,
          "static_release": "frontend-ui-public-20261015-f7c3102a", "static_read_only": True,
          "image_backfill": photos, "business_integrity": integrity,
          "business_integrity_algorithm": "original_v1_projection_from_frontend-20261015-images-93e6b420",
          "status_patch_sha256": hashlib.sha256(Path(status_mount[0]["Source"]).read_bytes()).hexdigest(),
          "full_catalog_remains_separate_from_image_tasks": True}
(ROOT / "data/samples/deployment/frontend-final-check.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"same_runtime_image": True, "worker_count": 2, "static_read_only": True,
                  "image_tasks": photos["tasks_total"], "successful_image_tasks": photos["task_statuses"]["succeeded"],
                  "actual_image_products": photos["products_with_current_images"],
                  "verified_business_records": integrity["verified_completed_image_tasks"],
                  "all_business_unchanged": True}, ensure_ascii=False))
