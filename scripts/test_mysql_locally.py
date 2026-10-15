"""Run the suite in a unique disposable local MySQL database, without copying secrets."""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    values = dotenv_values(args.env_file)
    try:
        url = make_url(values.get("DATABASE_URL") or "")
        if url.drivername != "mysql+pymysql" or url.host not in {"127.0.0.1", "localhost"}:
            raise ValueError
    except Exception:
        raise SystemExit("A local mysql+pymysql DATABASE_URL is required; connection details were not printed.") from None
    test_url = url.set(database="tiktok_shop_monitor_test_local_" + uuid4().hex[:12])
    options = args.pytest_args
    if options[:1] == ["--"]:
        options = options[1:]
    result = subprocess.run([sys.executable, "-m", "pytest", *(options or ["-q"])], cwd=ROOT,
        env={**os.environ, "MYSQL_TEST_URL": test_url.render_as_string(hide_password=False)},
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    output = result.stdout + result.stderr
    for value in (test_url.render_as_string(hide_password=False), values.get("DATABASE_URL"), url.password):
        if value:
            output = output.replace(value, "[local test connection redacted]")
    print(output, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
