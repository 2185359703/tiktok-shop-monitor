"""Publish a UI-only revision after proving its backend files match production."""

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def backend_fingerprint(source):
    paths = [source / name for name in ("Dockerfile", "requirements.txt", "pyproject.toml", "main.py")]
    if not (source / "app/main.py").is_file() or any(not path.is_file() for path in paths):
        raise ValueError("Incomplete application source")
    for folder in ("app", "scripts", "configs", "migrations", "data/seed"):
        for path in (source / folder).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.is_relative_to(source / "app/frontend") and path.suffix != ".py":
                continue
            paths.append(path)
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(source).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    import fcntl
    if not re.fullmatch(r"frontend-[a-z0-9-]{1,60}", args.release_id) or not re.fullmatch(r"[a-f0-9]{40}", args.commit):
        raise ValueError("Invalid release identity")
    root = args.project_root.resolve()
    source = (root / "releases" / args.release_id).resolve()
    if not source.is_relative_to(root / "releases"):
        raise ValueError("Source is outside this project's releases")
    with (root / "data/samples/deployment/async-rollout.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        active_path = root / "deployment/active-read-model-release.json"
        active = json.loads(active_path.read_text())
        record_path = Path(active["release_record"]).resolve()
        if not record_path.is_relative_to(root / "data/samples/deployment"):
            raise ValueError("Backend record is outside this project")
        record = json.loads(record_path.read_text())
        previous = (root / "releases" / record["release_id"]).resolve()
        if not previous.is_relative_to(root / "releases") or record["phase"] != "live":
            raise ValueError("A stable live backend release is required")
        fingerprint = backend_fingerprint(source)
        if fingerprint != backend_fingerprint(previous):
            raise ValueError("Backend files changed; use a full read-model release")
        ids = subprocess.check_output(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=tiktok-shop-monitor"], text=True).split()
        if not ids:
            raise ValueError("Project services are absent")
        output = subprocess.check_output(["docker", "inspect", "--format",
            '{{index .Config.Labels "com.docker.compose.service"}}|{{.Config.Image}}|{{index .Config.Labels "org.opencontainers.image.revision"}}', *ids], text=True)
        required = {"api", "worker", "scheduler", "collection", "analytics", "analytics-summaries", "analytics-heavy"}
        services = [line.split("|") for line in output.splitlines() if line.split("|")[0] in required]
        if {item[0] for item in services} != required or sum(item[0] == "worker" for item in services) != 2 or any(
            item[1] != active["image"] or item[2] != active["commit"] for item in services):
            raise ValueError("Application services do not match the live backend")
        spec = importlib.util.spec_from_file_location("ui_publisher", source / "deployment/rollout_async.py")
        publisher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(publisher)
        publisher.ROOT = root
        publisher.publish_ui(source, args.commit)
        active.update(frontend_commit=args.commit, frontend_source=str(source), frontend_backend_fingerprint=fingerprint,
                      frontend_published_at=datetime.now(timezone.utc).isoformat())
        active_path.write_text(json.dumps(active, indent=2) + "\n")
        print(json.dumps({"frontend_commit": args.commit, "backend_commit": active["commit"], "backend_files_identical": True}))


if __name__ == "__main__":
    main()
