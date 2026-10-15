"""Freeze a secret-free, hash-checked source release for frontend deployment."""

import argparse
import hashlib
import json
import re
import shutil
import stat
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS = {".py", ".js", ".css", ".html", ".toml", ".sql", ".yml", ".yaml", ".md", ".txt", ".woff2", ".ps1",
              ".png", ".svg", ".ico", ".jpg", ".jpeg", ".webp"}
DIRECTORIES = ("app", "scripts", "configs", "migrations", "monitoring", "tests", "deployment", "docs", "data/seed")
ROOT_FILES = ("Dockerfile", ".dockerignore", "requirements.txt", "pyproject.toml", "main.py", "README.md",
              "data/seed/shop_replacements_20261013.json")


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files():
    paths = [ROOT / name for name in ROOT_FILES]
    for folder in DIRECTORIES:
        for path in (ROOT / folder).rglob("*"):
            if "__pycache__" in path.parts or not path.is_file():
                continue
            if path.suffix in EXTENSIONS or (folder == "data/seed" and path.suffix == ".csv") or (folder == "tests" and path.suffix == ".json") or path.name.startswith("Dockerfile"):
                paths.append(path)
    return sorted(set(paths))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id")
    args = parser.parse_args()
    if args.release_id and not re.fullmatch(r"frontend-[a-z0-9-]{1,54}", args.release_id):
        raise ValueError("发布名称只能使用frontend-前缀、小写字母、数字和连字符。")
    files = source_files()
    before = {}
    for path in files:
        info = path.stat(follow_symlinks=False)
        if path.is_symlink() or info.st_nlink > 1 or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("发布文件不能来自链接或重解析路径。")
        before[path] = fingerprint(path)
    release_id = args.release_id or "frontend-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    parent = ROOT / "data/samples/deployment"
    directory = parent / "releases" / release_id
    directory.mkdir(parents=True, exist_ok=False)
    manifest = []
    for path in files:
        relative = path.relative_to(ROOT)
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied = fingerprint(target)
        if copied != before[path]:
            raise ValueError("源码在冻结期间发生变化，请重新建立发布快照。")
        manifest.append({"path": relative.as_posix(), "sha256": copied, "bytes": target.stat().st_size})
    if files != source_files() or any(fingerprint(path) != before[path] for path in files):
        raise ValueError("源码在冻结期间发生变化，请重新建立发布快照。")
    image = "tiktok-shop-monitor:" + release_id.lower()
    record = {"release_id": release_id, "image": image, "created_at": datetime.now(timezone.utc).isoformat(),
              "source_root": str(directory), "secret_files_included": False, "files": manifest}
    (directory / "release-manifest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    archive = parent / (release_id + ".tar.gz")
    with tarfile.open(archive, "w:gz") as stream:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                stream.add(path, arcname=path.relative_to(directory).as_posix(), recursive=False)
    result = {key: record[key] for key in ("release_id", "image", "created_at", "source_root", "secret_files_included")}
    result.update(archive=str(archive), archive_sha256=fingerprint(archive), source_files=len(manifest))
    (parent / "frontend-release.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
