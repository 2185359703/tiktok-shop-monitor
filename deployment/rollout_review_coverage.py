"""Roll a review-only overlay through the two existing coroutine workers."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess


ROOT = Path("/opt/tiktok-shop-monitor")


def run(arguments):
    result = subprocess.run(arguments, cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Worker rollout command failed; connection configuration was not printed.")
    return result.stdout.strip()


def workers():
    ids = run(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=tiktok-shop-monitor",
               "--filter", "label=com.docker.compose.service=worker"]).split()
    return [json.loads(run(["docker", "inspect", identifier]))[0] for identifier in ids]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Continue a partial rollout of this same review image.")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", args.commit) or not args.image.startswith("tiktok-shop-monitor:reviews-"):
        raise ValueError("Expected a committed project review image.")
    # The older base compose file requires this even when its service images
    # are overridden by the current live compose overlay.
    os.environ["APP_IMAGE"] = args.image
    target = json.loads(run(["docker", "image", "inspect", args.image]))[0]
    if target["Config"]["Labels"].get("org.opencontainers.image.revision") != args.commit:
        raise ValueError("Image revision does not match the reviewed commit.")
    evidence = ROOT / "data/samples/deployment"
    evidence.mkdir(parents=True, exist_ok=True)
    with (evidence / "async-rollout.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = workers()
        allowed = {args.base_image, args.image} if args.resume else {args.base_image}
        if len(before) != 2 or any(row["Config"]["Image"] not in allowed for row in before):
            raise ValueError("Expected exactly two running workers on the inspected base image.")
        labels = before[0]["Config"]["Labels"]
        files = labels["com.docker.compose.project.config_files"].split(",")
        if any(row["Config"]["Labels"]["com.docker.compose.project.config_files"] != ",".join(files) for row in before):
            raise ValueError("Worker compose configurations differ.")
        overlay = ROOT / "deployment/live-read-models.json"
        if Path(files[-1]).resolve() != overlay.resolve():
            raise ValueError("The inspected live compose overlay has changed.")
        env_file = labels["com.docker.compose.project.environment_file"]
        compose = ["docker", "compose", "--project-name", "tiktok-shop-monitor"]
        for file in files:
            if not Path(file).resolve().is_relative_to(ROOT):
                raise ValueError("Compose configuration is outside the project.")
            compose.extend(["-f", file])
        compose.extend(["--env-file", env_file])
        config = json.loads(overlay.read_text())
        if config["services"]["worker"]["image"] != (args.image if args.resume else args.base_image):
            raise ValueError("Configured worker image has changed.")
        # Resolve required variables and validate mounts before stopping any
        # healthy consumer, including during a resumed release.
        run([*compose, "config", "--quiet"])
        print(json.dumps({"phase": "verified", "commit": args.commit, "image": args.image,
                          "workers": [row["Name"] for row in before], "execute": args.execute}), flush=True)
        if not args.execute:
            return
        # Keep all other current services, mounts, limits and environment fields.
        config["services"]["worker"]["image"] = args.image
        overlay.write_text(json.dumps(config, indent=2) + "\n")
        for old in before:
            if old["Image"] == target["Id"]:
                continue
            run(["docker", "stop", "--time", "150", old["Id"]])
            run(["docker", "rm", old["Id"]])
            run([*compose, "up", "-d", "--no-deps", "--no-build", "--no-recreate", "--scale", "worker=2", "worker"])
            current = workers()
            if len(current) != 2 or not any(row["Image"] == target["Id"] for row in current):
                raise RuntimeError("Worker replacement did not become running; rollout stopped.")
            print(json.dumps({"phase": "worker_replaced", "previous": old["Name"]}), flush=True)
        after = workers()
        if any(row["Image"] != target["Id"] for row in after):
            raise RuntimeError("Workers did not converge to the review image.")
        record = {"commit": args.commit, "image": args.image, "base_image": args.base_image,
                  "deployed_at": datetime.now(timezone.utc).isoformat(),
                  "workers": [{"name": row["Name"], "image_id": row["Image"]} for row in after],
                  "other_services_recreated": False, "data_volumes_removed": False}
        (evidence / "review-coverage-rollout.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps({"phase": "complete", **record}), flush=True)


if __name__ == "__main__":
    main()
