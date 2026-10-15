"""Generate server-local credentials once, without displaying any secret."""

import json
import os
import secrets
from pathlib import Path


def main():
    target = Path(__file__).resolve().parents[1] / "deployment.env"
    if target.exists():
        if not target.is_file(): raise ValueError("部署配置路径不是普通文件。")
        print(json.dumps({"existing_config_preserved": True}))
        return
    mysql_password, root_password, broker_password = (secrets.token_hex(24) for _ in range(3))
    values = {"MYSQL_PASSWORD": mysql_password, "MYSQL_ROOT_PASSWORD": root_password,
              "RABBITMQ_PASSWORD": broker_password, "API_TOKEN": secrets.token_hex(32),
              "DATABASE_URL": f"mysql+pymysql://tiktok_monitor:{mysql_password}@mysql:3306/tiktok_shop_monitor?charset=utf8mb4",
              "RABBITMQ_URL": f"amqp://tiktok_monitor:{broker_password}@rabbitmq:5672/%2F"}
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
    print(json.dumps({"created": True, "secret_values_displayed": False}))


if __name__ == "__main__":
    main()
