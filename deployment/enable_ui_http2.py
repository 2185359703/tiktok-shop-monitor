"""Enable HTTP/2 for this project's existing HTTPS vhost without a restart."""

import argparse
import json
import re
import shutil
import subprocess
import time
from pathlib import Path


def enable(content, hostname):
    allowed = {hostname, "www." + hostname}
    names = re.findall(r"(?m)^\s*server_name\s+([^;]+);", content)
    if not names or any(not set(value.split()).issubset(allowed) for value in names):
        raise ValueError("The site file contains another application's hostname")
    count = 0
    def replace(match):
        nonlocal count
        options = match.group(2).split()
        if "ssl" not in options:
            raise ValueError("Expected an existing TLS listener")
        count += 1
        return match.group(0) if "http2" in options else match.group(1) + match.group(2) + " http2;"
    result = re.sub(r"(?m)^(\s*listen\s+(?:\[::\]:)?443\s+)([^;]+);", replace, content)
    if not count:
        raise ValueError("The site does not have an HTTPS listener")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-name", default="shopscope-monitor.top")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9.-]+", args.server_name):
        raise ValueError("Invalid server name")
    path = (Path("/etc/nginx/sites-enabled") / args.server_name).resolve()
    if not path.is_relative_to(Path("/etc/nginx")) or not path.is_file():
        raise ValueError("Expected the existing project nginx site")
    version = subprocess.run(["nginx", "-V"], capture_output=True, text=True, check=True)
    if "--with-http_v2_module" not in version.stderr:
        raise ValueError("nginx was built without HTTP/2")
    original = path.read_text()
    updated = enable(original, args.server_name)
    changed = original != updated
    backup = None
    if args.execute and changed:
        backup = args.project_root.resolve() / "data/samples/deployment" / f"nginx-http2-before-{time.time_ns()}.conf"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        backup.chmod(0o600)
        try:
            path.write_text(updated)
            subprocess.run(["nginx", "-t"], capture_output=True, text=True, check=True)
            subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True, check=True)
        except Exception:
            path.write_text(original)
            raise
    print(json.dumps({"execute": args.execute, "changed": changed, "site": str(path),
                      "backup": str(backup) if backup else None, "nginx_restarted": False}))


if __name__ == "__main__":
    main()
