"""Freeze the frontend alone for a read-only static mount on the tested API."""

import argparse
import hashlib
import json
import re
import shutil
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source-root", type=Path, required=True)
parser.add_argument("--release-id", required=True)
args = parser.parse_args()
source_root = args.source_root.resolve()
if not source_root.is_relative_to(ROOT):
    raise ValueError("发布来源必须在本项目目录内。")
source = source_root / "app/frontend"
if not (source / "index.html").is_file() or not (source / "api.js").is_file():
    raise ValueError("发布来源缺少前端入口。")
identifier = args.release_id
if not re.fullmatch(r"frontend-ui-[a-z0-9-]+", identifier):
    raise ValueError("发布标识不符合项目约定。")
target = ROOT / "data/samples/deployment/ui-releases" / identifier
target.mkdir(parents=True, exist_ok=False)
records = []
for path in sorted(source.rglob("*")):
    if not path.is_file():
        continue
    if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
        raise ValueError("前端资源必须属于冻结目录。")
    relative = Path("app/frontend") / path.relative_to(source)
    destination = target / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = path.read_bytes()
    shutil.copy2(path, destination)
    if destination.read_bytes() != data or path.read_bytes() != data:
        raise ValueError("静态资源在冻结期间发生变化。")
    records.append({"path": relative.as_posix(), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
manifest = {"release_id": identifier, "runtime_image": "tiktok-shop-monitor:frontend-20261015-images-verified-c7a93e14",
            "mount_target": "/opt/tiktok-shop-monitor/app/frontend", "files": records}
(target / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
archive = ROOT / "data/samples/deployment" / (identifier + ".tar.gz")
with tarfile.open(archive, "w:gz") as stream:
    for path in sorted(target.rglob("*")):
        if path.is_file():
            stream.add(path, arcname=path.relative_to(target).as_posix(), recursive=False)
print(json.dumps({"release_id": identifier, "archive": str(archive),
                  "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "static_assets": len(records)}))
