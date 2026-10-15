"""Show a bounded, credential-redacted test diagnostic from the server."""

import argparse
import re
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("--head", action="store_true")
parser.add_argument("--errors-only", action="store_true")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
content = (root / "data/samples/deployment/frontend-image-tests.log").read_text(encoding="utf-8")
values = dict(line.split("=", 1) for line in (root / "deployment.env").read_text().splitlines()
              if line and not line.startswith("#"))
for value in sorted(values.values(), key=len, reverse=True):
    if value:
        content = content.replace(value, "<redacted>")
content = re.sub(r"(?:mysql(?:\+pymysql)?|redis|amqps?)://[^\s'\"]+", "<redacted-url>", content)
lines = content.splitlines()
if args.errors_only:
    lines = [line for line in lines if line.startswith(("E ", "FAILED ", "> "))][:160]
else:
    lines = lines[:200] if args.head else lines[-110:]
print("\n".join(lines))
