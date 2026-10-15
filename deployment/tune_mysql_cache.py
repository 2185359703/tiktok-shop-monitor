"""Apply the existing 4 GiB VPS's project-specific database memory budget."""

import argparse
import json
import os
import subprocess
from pathlib import Path


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
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
    if args.execute:
        if os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") < 3_500_000_000:
            raise ValueError("This profile requires the confirmed 4 GiB host.")
        run(["docker", "update", "--memory", "1g", "--memory-swap", "1g", container])
        sql("SET GLOBAL innodb_buffer_pool_size=536870912")
    active = sql("SELECT COMMAND, TIME, STATE FROM information_schema.PROCESSLIST WHERE COMMAND <> 'Sleep' AND ID <> CONNECTION_ID()")
    print(json.dumps({"mode": "execute" if args.execute else "preview", "buffer_pool_before_bytes": before,
                      "buffer_pool_after_bytes": int(sql("SELECT @@GLOBAL.innodb_buffer_pool_size")),
                      "target_bytes": 536870912, "container_memory_limit_bytes": 1073741824 if args.execute else details["HostConfig"]["Memory"],
                      "active_statement_states": active.splitlines(), "schema_or_data_writes": 0,
                      "server_restarted": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "Project database cache update did not finish."}))
        raise SystemExit(1)
