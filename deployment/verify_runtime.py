"""Run the project suite in the deployed image against disposable test data."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4


def run(arguments, **options):
    return subprocess.run(arguments, check=True, **options)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="tiktok-shop-monitor:server")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--log-name", default="image-tests.log")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source_root = args.source_root.resolve() if args.source_root else root
    if not source_root.is_relative_to(root) or not (source_root / "tests/conftest.py").is_file():
        raise ValueError("检查源码必须位于本项目内并包含隔离测试入口。")
    if Path(args.log_name).name != args.log_name or not args.log_name.endswith(".log"):
        raise ValueError("检查日志必须是单个 .log 文件名。")
    values = dict(line.split("=", 1) for line in (root / "deployment.env").read_text().splitlines() if line and not line.startswith("#"))
    compose = ["docker", "compose", "-f", "deployment/compose.server.yml", "--env-file", "deployment.env"]
    mysql_id = run([*compose, "ps", "-q", "mysql"], cwd=root, capture_output=True, text=True).stdout.strip()
    if not mysql_id: raise ValueError("项目 MySQL 容器尚未启动。")
    labels = json.loads(run(["docker", "inspect", "--format", "{{json .Config.Labels}}", mysql_id], capture_output=True, text=True).stdout)
    if labels.get("com.docker.compose.project") != "tiktok-shop-monitor" or labels.get("com.docker.compose.service") != "mysql":
        raise ValueError("MySQL 容器不属于本项目。")
    identifier = uuid4().hex[:12]
    redis_name = f"tiktok-shop-runtime-test-redis-{identifier}"
    environment = {**os.environ,
        "MYSQL_TEST_URL": f"mysql+pymysql://root:{values['MYSQL_ROOT_PASSWORD']}@127.0.0.1:3306/tiktok_shop_monitor_test_server_{identifier}?charset=utf8mb4",
        "REDIS_TEST_URL": "redis://127.0.0.1:36387/0"}
    created = False
    try:
        run(["docker", "run", "-d", "--name", redis_name, "--label", "codex.project=tiktok-shop-monitor-runtime-test",
             "--network", f"container:{mysql_id}", "redis:7.4-alpine", "redis-server", "--bind", "127.0.0.1",
             "--port", "36387", "--save", "", "--appendonly", "no"], capture_output=True)
        created = True
        result = subprocess.run(["docker", "run", "--rm", "--user", "0", "--network", f"container:{mysql_id}",
             "--mount", f"type=bind,source={source_root / 'tests'},target=/opt/tiktok-shop-monitor/tests,readonly",
             "--env", "MYSQL_TEST_URL", "--env", "REDIS_TEST_URL", args.image, "sh", "-c",
             "python -m pip install --quiet 'pytest>=8,<9' && python -m pytest -q"], env=environment, capture_output=True, text=True)
        evidence = root / "data/samples/deployment"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / args.log_name).write_text(result.stdout + result.stderr, encoding="utf-8")
        # No raw exceptions, process environment or connection strings in console.
        summaries = [line for line in result.stdout.splitlines() if " passed" in line or " failed" in line or " skipped" in line]
        print(json.dumps({"passed": result.returncode == 0, "summary": summaries[-1:]}, ensure_ascii=False))
        if result.returncode: raise RuntimeError("部署镜像回归检查未通过，详见受控本地日志。")
    finally:
        if created:
            run(["docker", "rm", "-f", "-v", redis_name], capture_output=True)


if __name__ == "__main__":
    main()
