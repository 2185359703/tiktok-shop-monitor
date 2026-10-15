"""Create one isolated temporary RabbitMQ container, with protected local secrets."""

import json
import os
import secrets
import subprocess
from pathlib import Path
from uuid import uuid4

from app.storage.database import PROJECT_ROOT


def main():
    identifier = uuid4().hex[:12]
    directory = PROJECT_ROOT / f"data/samples/broker-check/{identifier}"
    directory.mkdir(parents=True)
    config_file = directory / "runtime.env"
    password, vhost = secrets.token_urlsafe(32), f"tiktok_check_{identifier}"
    container = f"tiktok-shop-broker-check-{identifier}"
    config_file.write_text(f"RABBITMQ_DEFAULT_USER=monitor_check\nRABBITMQ_DEFAULT_PASS={password}\nRABBITMQ_DEFAULT_VHOST={vhost}\n"
                           f"RABBITMQ_TEST_URL=amqp://monitor_check:{password}@127.0.0.1:5679/{vhost}\n", encoding="utf-8")
    if os.name == "nt":
        user = subprocess.run(["whoami"], check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["icacls", str(config_file), "/inheritance:r", "/grant:r", f"{user}:(F)"], check=True, capture_output=True)
    else:
        config_file.chmod(0o600)
    result = subprocess.run(["docker", "run", "-d", "--name", container, "--label", "codex.project=tiktok-shop-monitor-broker-check",
                             "--env-file", str(config_file), "-p", "127.0.0.1:5679:5672", "rabbitmq:4.1-management"],
                             capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("隔离 Broker 容器启动失败。")
    metadata = {"container": container, "container_id": result.stdout.strip(), "port": 5679,
                "config_file": str(config_file), "directory": str(directory), "project_label": "tiktok-shop-monitor-broker-check"}
    (directory / "container.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"container": container, "metadata": str(directory / 'container.json')}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "临时 Broker 创建未完成。"}, ensure_ascii=False))
        raise SystemExit(1)
