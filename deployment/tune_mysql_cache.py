"""Apply the existing 4 GiB VPS's project-specific database memory budget."""

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.project_root.resolve()
    compose = ["docker", "compose", "-f", "deployment/compose.server.yml", "--env-file", "deployment.env"]
    container = run([*compose, "ps", "-q", "mysql"], cwd=root)
    if not container:
        raise ValueError("Project MySQL is absent.")
    details = json.loads(run(["docker", "inspect", container]))[0]
    labels = details["Config"]["Labels"]
    if labels.get("com.docker.compose.project") != "tiktok-shop-monitor" or labels.get("com.docker.compose.service") != "mysql":
        raise ValueError("Container ownership does not match.")
    values = dict(line.split("=", 1) for line in (root / "deployment.env").read_text().splitlines() if line and not line.startswith("#"))
    environment = {**os.environ, "MYSQL_PWD": values["MYSQL_ROOT_PASSWORD"]}
    def sql(statement):
        return run(["docker", "exec", "--env", "MYSQL_PWD", container, "mysql", "--user=root", "--batch",
                    "--skip-column-names", "--execute", statement], env=environment)
    before = int(sql("SELECT @@GLOBAL.innodb_buffer_pool_size"))
    memory_limit = 1280 * 1024 * 1024
    compose_path = root / "deployment/compose.server.yml"
    original = compose_path.read_text()
    block = re.search(r"(?ms)^  mysql:\n.*?(?=^  [a-zA-Z0-9_-]+:|\Z)", original)
    if block is None:
        raise ValueError("Expected exactly one project MySQL service block")
    updated, commands = re.subn(r"--innodb-buffer-pool-size=\d+", "--innodb-buffer-pool-size=536870912", block.group())
    updated, limits = re.subn(r"(?m)^    mem_limit:.*$", "    mem_limit: 1280m", updated)
    if commands != 1 or limits != 1:
        raise ValueError("Unexpected MySQL service configuration")
    persistent = original[:block.start()] + updated + original[block.end():]
    backup = None
    if args.execute:
        if os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") < 3_500_000_000:
            raise ValueError("This profile requires the confirmed 4 GiB host.")
        if original != persistent:
            backup = root / "data/samples/deployment" / f"mysql-memory-before-{time.time_ns()}.yml"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(compose_path, backup)
            backup.chmod(0o600)
        run(["docker", "update", "--memory", "1280m", "--memory-swap", "1280m", container])
        if before != 536870912:
            sql("SET GLOBAL innodb_buffer_pool_size=536870912")
        if original != persistent:
            compose_path.write_text(persistent)
    active = sql("SELECT COMMAND, TIME, STATE FROM information_schema.PROCESSLIST WHERE COMMAND <> 'Sleep' AND ID <> CONNECTION_ID()")
    print(json.dumps({"mode": "execute" if args.execute else "preview", "buffer_pool_before_bytes": before,
                      "buffer_pool_after_bytes": int(sql("SELECT @@GLOBAL.innodb_buffer_pool_size")),
                      "target_bytes": 536870912, "container_memory_before_bytes": details["HostConfig"]["Memory"],
                      "container_memory_limit_bytes": memory_limit if args.execute else details["HostConfig"]["Memory"],
                      "compose_updated": bool(args.execute and original != persistent), "compose_backup": str(backup) if backup else None,
                      "active_statement_states": active.splitlines(), "schema_or_data_writes": 0,
                      "server_restarted": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "Project database cache update did not finish."}))
        raise SystemExit(1)
