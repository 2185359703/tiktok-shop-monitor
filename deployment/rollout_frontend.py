"""Roll a verified image through this project's UI and image-capable workers."""

import argparse
import json
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", "deployment/compose.server.yml", "-f", "deployment/compose.frontend.yml",
           "--env-file", "deployment.env"]
INSPECT = ('{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},'
           '"labels":{{json .Config.Labels}},"runtime":{{json .State}}}')


def run(arguments):
    result = subprocess.run(arguments, cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("部署命令未完成；未打印容器环境或连接信息。")
    return result.stdout.strip()


def containers(service):
    identifiers = run([*COMPOSE, "ps", "-a", "-q", service]).split()
    result = []
    for identifier in identifiers:
        data = json.loads(run(["docker", "inspect", "--format", INSPECT, identifier]))
        labels = data.pop("labels")
        runtime = data.pop("runtime")
        data.update(state=runtime["Status"], health=runtime.get("Health", {}).get("Status"))
        if labels.get("com.docker.compose.project") != "tiktok-shop-monitor" or labels.get("com.docker.compose.service") != service:
            raise ValueError("容器不属于本项目指定服务。")
        result.append(data)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id", required=True)
    args = parser.parse_args()
    directory = (ROOT / "releases" / args.release_id).resolve()
    if directory.parent != (ROOT / "releases").resolve():
        raise ValueError("发布目录超出本项目。")
    manifest = json.loads((directory / "release-manifest.json").read_text(encoding="utf-8"))
    image = manifest["image"]
    if not image.startswith("tiktok-shop-monitor:frontend-"):
        raise ValueError("仅支持本项目已冻结的前端发布镜像。")
    expected = run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    # Parse in memory and retain only image names; never print environment data.
    configured_images = {name: value["image"] for name, value in
                         json.loads(run([*COMPOSE, "config", "--format", "json"]))["services"].items()
                         if name in {"api", "worker", "scheduler", "collection"}}
    if len(configured_images) != 4 or set(configured_images.values()) != {image}:
        raise ValueError("API、Worker、Scheduler、Collection必须指向同一候选镜像。")
    before = {service: containers(service) for service in ("api", "worker", "scheduler", "collection")}
    if len(before["worker"]) != 2 or any(item["state"] != "running" for item in before["worker"]):
        raise ValueError("滚动更新前必须有且仅有两个运行中的项目Worker。")
    print(json.dumps({"phase": "verified", "release_id": args.release_id, "image_id": expected}), flush=True)
    run([*COMPOSE, "up", "-d", "--no-deps", "--no-build", "api"])
    deadline = time.monotonic() + 90
    while True:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/health/ready", timeout=5) as response:
                if response.status == 200:
                    break
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("新API未通过就绪检查，尚未更新采集进程。")
        time.sleep(1)
    print(json.dumps({"phase": "api_ready"}), flush=True)
    for old in before["worker"]:
        if old["image"] == expected:
            continue
        # Remove one explicitly validated stateless worker at a time. Named
        # evidence volumes, queues, attempts and checkpoints remain attached.
        run(["docker", "stop", "--time", "40", old["id"]])
        run(["docker", "rm", old["id"]])
        run([*COMPOSE, "up", "-d", "--no-deps", "--no-build", "--no-recreate", "--scale", "worker=2", "worker"])
        active = containers("worker")
        if len(active) != 2 or any(item["state"] != "running" for item in active):
            raise RuntimeError("Worker数量或运行状态不正确，暂停后续更新。")
        print(json.dumps({"phase": "worker_replaced", "previous_container": old["name"]}), flush=True)
    run([*COMPOSE, "up", "-d", "--no-deps", "--no-build", "scheduler", "collection"])
    after = {service: containers(service) for service in before}
    if any(item["image"] != expected or item["state"] != "running" for values in after.values() for item in values):
        raise RuntimeError("部分应用服务尚未运行相同镜像；未启动图片任务。")
    result = {"release_id": args.release_id, "image": image, "image_id": expected,
              "deployed_at": datetime.now(timezone.utc).isoformat(), "before": before, "after": after,
              "worker_count": 2, "image_tasks_enqueued": False,
              "infrastructure_recreated": False, "business_tables_truncated": False}
    path = ROOT / "data/samples/deployment/frontend-rollout.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"phase": "complete", "release_id": args.release_id, "worker_count": 2}), flush=True)


if __name__ == "__main__":
    main()
