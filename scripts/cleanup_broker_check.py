"""Remove only the container whose saved ID and project label still match."""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from scripts.verify_queue_runtime import checked_metadata


def main():
    parser = argparse.ArgumentParser(description="清理本项目的临时 Broker，保留联调证据与报告")
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args()
    metadata = checked_metadata(args.metadata)
    directory, config = Path(metadata["directory"]).resolve(), Path(metadata["config_file"]).resolve()
    if directory != args.metadata.resolve().parent or config != directory / "runtime.env":
        raise ValueError("临时配置路径与本次验证目录不一致。")
    subprocess.run(["docker", "rm", "-f", "-v", metadata["container_id"]], check=True, capture_output=True)
    config.unlink(missing_ok=True)
    evidence = {"cleaned_at": datetime.now(timezone.utc).isoformat(), "container": metadata["container"],
                "temporary_container_removed": True, "temporary_config_removed": True,
                "docker_engine_stopped": False, "reports_and_evidence_retained": True}
    (directory / "cleanup.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "临时 Broker 清理未完成，请核对容器身份。"}, ensure_ascii=False))
        raise SystemExit(1)
