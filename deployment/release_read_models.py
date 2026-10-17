"""Stage a committed read-model release, warm it, then switch the public API.

Run on the application host after uploading a git archive and building/testing
its image. Credentials stay in the existing deployment.env and are never logged.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import tomllib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


SERVICES = ("api", "worker", "scheduler", "collection")
ANALYTICS_SERVICES = ("analytics", "analytics-summaries", "analytics-heavy")
# Repair existing collection columns before analytics validates every model table.
UPGRADE_MIGRATIONS = ("scripts.migrate_collection_management", "scripts.migrate_collection_monitor_indexes",
                      "scripts.migrate_collection_plan_archive", "scripts.migrate_collection_monitor_rollups",
                      "scripts.migrate_collection_launches", "scripts.migrate_collection_alerts",
                      "scripts.migrate_analytics_rollups", "scripts.migrate_recent_index")


def upgrade_role_ready(name, items):
    """A completed fixed batch is healthy; persistent runtime roles must be live."""
    return bool(items) and all(
        item["State"]["Running"] or (
            name == "collection" and item["State"].get("Status") == "exited"
            and item["State"].get("ExitCode") == 0 and not item["State"].get("OOMKilled", False)
        ) for item in items
    )


class Release:
    def __init__(self, root, identifier, commit):
        self.root = root.resolve()
        if not re.fullmatch(r"frontend-[a-z0-9-]{1,60}", identifier) or not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise ValueError("Invalid release identity")
        self.identifier, self.commit = identifier, commit
        self.source = self.root / "releases" / identifier
        if not (self.source / "app/main.py").is_file() or not (self.root / "deployment.env").is_file():
            raise ValueError("Release source or protected environment is missing")
        self.image = "tiktok-shop-monitor:" + identifier
        self.evidence = self.root / "data/samples/deployment" / identifier
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.record_path = self.evidence / "release-state.json"
        self.record = json.loads(self.record_path.read_text()) if self.record_path.exists() else {}
        self.environment = dict(os.environ)
        if self.record and self.record["commit"] != commit:
            raise ValueError("Release identity was already used by another commit")
        files = self.record.get("base_files")
        if not files:
            ids = self.run(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=tiktok-shop-monitor",
                            "--filter", "label=com.docker.compose.service=api"]).split()
            if len(ids) != 1:
                raise ValueError("Expected exactly one live project API")
            labels = json.loads(self.run(["docker", "inspect", "--format", "{{json .Config.Labels}}", ids[0]]))
            files = labels.get("com.docker.compose.project.config_files", "").split(",")
            self.environment["APP_IMAGE"] = self.run(["docker", "inspect", "--format", "{{.Config.Image}}", ids[0]])
        else:
            self.environment["APP_IMAGE"] = self.record["previous_images"]["api"][0]
        # The live overlay is a mutable output, never an input to the next release.
        self.base_files = [Path(name).resolve() for name in files if name and
                           Path(name).resolve() != self.root / "deployment/live-read-models.json"]
        if not self.base_files or any(not path.is_relative_to(self.root) or not path.is_file() for path in self.base_files):
            raise ValueError("Live Compose files must belong to this project")
        self.base = ["docker", "compose", *[part for path in self.base_files for part in ("-f", str(path))],
                     "--env-file", str(self.root / "deployment.env")]
        self.overlay = self.root / "deployment/live-read-models.json"
        self.compose = [*self.base, "-f", str(self.overlay)]

    def run(self, argv, *, timeout=None):
        result = subprocess.run(argv, cwd=self.root, env=self.environment, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            log = self.evidence / "last-command-error.txt"
            log.touch(mode=0o600, exist_ok=True)
            log.chmod(0o600)
            log.write_text(result.stderr, encoding="utf-8")
            raise RuntimeError(f"Command failed ({argv[0]}); see protected release log")
        return result.stdout.strip()

    def save(self, phase, **values):
        self.record.update(release_id=self.identifier, commit=self.commit, image=self.image, phase=phase,
                           updated_at=datetime.now(timezone.utc).isoformat(), **values)
        self.record_path.write_text(json.dumps(self.record, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"phase": phase, **values}, ensure_ascii=False), flush=True)

    def containers(self, service):
        ids = self.run(["docker", "ps", "-a", "-q", "--filter", "label=com.docker.compose.project=tiktok-shop-monitor",
                        "--filter", "label=com.docker.compose.service=" + service]).split()
        result = []
        for identifier in ids:
            value = json.loads(self.run(["docker", "inspect", "--format", "{{json .}}", identifier]))
            labels = value["Config"]["Labels"]
            if labels.get("com.docker.compose.project") != "tiktok-shop-monitor" or labels.get("com.docker.compose.service") != service:
                raise ValueError("Container is outside this project")
            result.append(value)
        return result

    def read(self, path, *, port=8000, timeout=10):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
            return json.load(response)

    def up(self, *services, compose=None):
        scale = ["--scale", "worker=2"] if "worker" in services else []
        self.run([*(compose or self.compose), "up", "-d", "--no-build", "--no-deps", *scale, *services])

    def stop_sources(self):
        for service in ("collection", "scheduler", "worker"):
            for item in self.containers(service):
                if item["State"]["Running"]:
                    self.run(["docker", "stop", "--time", "300", item["Id"]], timeout=330)
            print(json.dumps({"phase": "writers_drained", "service": service}), flush=True)

    def runtime_config(self, api):
        values = dict(item.split("=", 1) for item in api["Config"]["Env"] if "=" in item)
        path = values.get("SETTINGS_FILE", "/opt/tiktok-shop-monitor/configs/server.toml")
        if not path.endswith(".toml"):
            raise ValueError("Expected a TOML runtime configuration")
        raw = self.run(["docker", "exec", api["Id"], "python", "-c", "from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text())", path])
        config = tomllib.loads(raw)
        forbidden = {"database_url", "rabbitmq_url", "redis_url", "api_token", "detail_proxy_url"}
        if any(forbidden.intersection(section) for section in config.values() if isinstance(section, dict)):
            raise ValueError("Connection secrets must not be placed in the runtime TOML")
        (self.evidence / "previous-server.toml").write_text(raw + "\n")
        active = json.loads(self.run(["docker", "exec", api["Id"], "python", "-c",
            "import json; from app.settings import Settings; s=Settings.load(); print(json.dumps({'worker_concurrency':getattr(s,'worker_concurrency',1),'enabled_task_types':list(s.enabled_task_types),'minimum_interval_seconds':s.minimum_interval_seconds}))"]))
        config.setdefault("runtime", {}).update(worker_concurrency=active["worker_concurrency"], analytics_concurrency=1,
                                                enabled_task_types=active["enabled_task_types"],
                                                monitor_expected_workers=2, monitor_expected_schedulers=1)
        config.setdefault("request", {})["minimum_interval_seconds"] = active["minimum_interval_seconds"]
        if values.get("ENABLED_TASK_TYPES") is not None:
            config["runtime"]["enabled_task_types"] = [v.strip() for v in values["ENABLED_TASK_TYPES"].split(",") if v.strip()]
        body = "\n".join("[" + section + "]\n" + "\n".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in items.items())
                         + "\n" for section, items in config.items())
        target = self.source / "runtime-server.toml"
        target.write_text(body, encoding="utf-8")
        target.chmod(0o644)
        return {"enabled_task_types": config["runtime"]["enabled_task_types"],
                "minimum_interval_seconds": config["request"]["minimum_interval_seconds"], "worker_concurrency": active["worker_concurrency"]}

    def backup_schema_and_control(self, mysql):
        path = self.evidence / "schema-and-control.sql.gz"
        if path.exists():
            path = self.evidence / f"schema-and-control-{time.time_ns()}.sql.gz"
        shell = 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump --user=root --single-transaction --quick --skip-lock-tables --column-statistics=0 '
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream, gzip.GzipFile(fileobj=stream, mode="wb") as output:
            for suffix in ("--no-data tiktok_shop_monitor", "--no-create-info tiktok_shop_monitor shops crawl_tasks"):
                process = subprocess.Popen(["docker", "exec", mysql["Id"], "sh", "-c", shell + suffix], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                shutil.copyfileobj(process.stdout, output)
                process.stdout.close()
                error = process.stderr.read()
                if process.wait() != 0:
                    raise RuntimeError("Protected database backup failed")
        path.chmod(0o600)
        return {"backup_path": str(path), "backup_bytes": path.stat().st_size,
                "backup_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def write_overlay(self, *, public=False):
        runtime = {"type": "bind", "source": str(self.source / "runtime-server.toml"),
                   "target": "/opt/tiktok-shop-monitor/configs/release-runtime.toml", "read_only": True}
        environment = {"ANALYTICS_ENABLED": "1", "SETTINGS_FILE": runtime["target"]}
        services = {name: {"image": self.image, "environment": environment, "volumes": [runtime], "stop_grace_period": "300s"}
                    for name in ("worker", "scheduler", "collection")}
        if not public:
            services["collection"]["command"] = ["python", "-m", "scripts.run_collection", "--poll-seconds", "300"]
        analysis_env = {**environment, "DATABASE_URL": "${DATABASE_URL:?Set DATABASE_URL}",
                        "REDIS_URL": "${REDIS_URL:-redis://redis:6379/0}", "ANALYTICS_CONCURRENCY": "1",
                        "DB_POOL_SIZE": "2", "DB_MAX_OVERFLOW": "1"}
        for name, role, cpus, memory in (("analytics", "projections", "0.25", "192m"),
                ("analytics-summaries", "summaries", "0.6", "256m"), ("analytics-heavy", "heavy", "0.15", "256m")):
            services[name] = {"image": self.image, "command": ["python", "-m", "scripts.run_analytics", "--role", role],
                "environment": analysis_env, "volumes": [runtime], "mem_limit": memory, "cpus": cpus, "restart": "unless-stopped",
                "stop_grace_period": "90s", "logging": {"driver": "json-file", "options": {"max-size": "5m", "max-file": "2"}}}
        services["api-candidate"] = {"image": self.image, "command": ["python", "-m", "scripts.run_api"],
            "environment": {**analysis_env, "API_HOST": "0.0.0.0", "API_PORT": "8000", "API_TOKEN": "${API_TOKEN:?Set API_TOKEN}",
                "REDIS_URL": "${REDIS_URL:-redis://redis:6379/0}", "DB_POOL_TIMEOUT": "3"},
            "volumes": [runtime, "report_exports:/opt/tiktok-shop-monitor/data/exports/reports:ro", "collection_state:/opt/tiktok-shop-monitor/data/collections:ro"],
            "ports": ["127.0.0.1:18081:8000"], "mem_limit": "384m", "restart": "unless-stopped"}
        if public:
            services["api"] = {"image": self.image, "environment": {**environment, "DB_POOL_TIMEOUT": "3"}, "volumes": [runtime,
                {"type": "bind", "source": str(self.source / "app/frontend"), "target": "/opt/tiktok-shop-monitor/app/frontend", "read_only": True},
                {"type": "bind", "source": str(self.source / "app/services/image_backfill.py"),
                 "target": "/opt/tiktok-shop-monitor/app/services/image_backfill.py", "read_only": True}]}
        self.overlay.write_text(json.dumps({"services": services}, indent=2) + "\n")

    def upgrade(self):
        """Stage an additive upgrade of an already-enabled read-model deployment."""
        if self.record.get("phase") in {"warming", "live"}:
            raise ValueError("Release already staged; use status or cutover")
        if self.record.get("phase") == "upgrade_prepared":
            raise ValueError("Interrupted upgrade; inspect the preserved rollback snapshot before retrying")
        if not self.overlay.is_file() or not self.read("/api/v2/ui/bootstrap").get("ready"):
            raise ValueError("Upgrade requires an enabled, ready public read model")
        revision = self.run(["docker", "image", "inspect", "--format", '{{index .Config.Labels "org.opencontainers.image.revision"}}', self.image])
        if revision != self.commit:
            raise ValueError("Candidate image does not identify the committed revision")
        previous = {name: self.containers(name) for name in (*SERVICES, "analytics")}
        for name in ANALYTICS_SERVICES[1:]:
            if items := self.containers(name):
                previous[name] = items
        if any(not upgrade_role_ready(name, items) for name, items in previous.items()):
            raise ValueError("Runtime roles must be running; a fixed collection batch may have exited successfully")
        if len(previous["worker"]) != 2 or any(len({item["Image"] for item in items}) != 1 for items in previous.values()):
            raise ValueError("Each application role must have one consistent image, with two workers")
        saved_overlay = self.evidence / "previous-live-overlay.json"
        shutil.copy2(self.overlay, saved_overlay)
        saved_overlay.chmod(0o600)
        api = previous["api"][0]
        preserved = self.runtime_config(api)
        networks = list(api["NetworkSettings"]["Networks"])
        if len(networks) != 1:
            raise ValueError("Expected one application network")
        self.save("upgrade_prepared", base_files=[str(path) for path in self.base_files],
            previous_images={name: [item["Config"]["Image"] for item in items] for name, items in previous.items()},
            previous_public_overlay=str(saved_overlay), preserved_runtime=preserved)
        for module in UPGRADE_MIGRATIONS:
            self.run(["docker", "run", "--rm", "--network", networks[0], "--env-file", str(self.root / "deployment.env"),
                self.image, "python", "-m", module, "--execute"], timeout=90)
        self.write_overlay(public=False)
        self.up(*ANALYTICS_SERVICES, "api-candidate")
        self.save("warming", public_api_changed=False, source_writers_changed=False)

    def prepare(self):
        if self.record.get("phase") in {"warming", "live"}:
            raise ValueError("Release is already active; use status or cutover")
        revision = self.run(["docker", "image", "inspect", "--format", '{{index .Config.Labels "org.opencontainers.image.revision"}}', self.image])
        if revision != self.commit:
            raise ValueError("Candidate image does not identify the committed revision")
        api = self.containers("api")[0]
        mysql = self.containers("mysql")[0]
        workers = self.containers("worker")
        if len(workers) != 2 or not all(w["State"]["Running"] for w in workers):
            raise ValueError("Expected two running production workers")
        if len({item["Image"] for name in SERVICES for item in self.containers(name)}) != 1:
            raise ValueError("Another application rollout is in progress; wait for consistent images")
        networks = list(api["NetworkSettings"]["Networks"])
        if len(networks) != 1:
            raise ValueError("Expected one application network")
        for index, path in enumerate(self.base_files):
            shutil.copy2(path, self.evidence / f"previous-{index}-{path.name}")
        preserved = self.runtime_config(api)
        self.save("preparing", previous_images={name: [c["Config"]["Image"] for c in self.containers(name)] for name in SERVICES},
                  preserved_runtime=preserved, base_files=[str(path) for path in self.base_files])
        try:
            self.stop_sources()
            backup = self.backup_schema_and_control(mysql)
            self.save("backed_up", **backup)
            for module in ("scripts.migrate_shop_profiles", "scripts.migrate_product_reviews", "scripts.migrate_analytics", "scripts.migrate_ui_indexes", *UPGRADE_MIGRATIONS):
                result = self.run(["docker", "run", "--rm", "--network", networks[0], "--env-file", str(self.root / "deployment.env"),
                                  "--env", "ANALYTICS_ENABLED=0", self.image, "python", "-m", module, "--execute"])
                self.save("migrating", migration=module, result=json.loads(result))
            self.write_overlay()
            self.up("worker", "scheduler", "collection", *ANALYTICS_SERVICES, "api-candidate")
            self.save("warming", public_api_changed=False, analytics_concurrency=1)
        except Exception:
            self.up("worker", "scheduler", "collection", compose=self.base)
            self.save("prepare_failed_old_writers_restored")
            raise

    def status(self):
        candidates = self.run([*self.compose, "ps", "-q", "analytics"]).split()
        if not candidates:
            raise ValueError("Analytics service has not been started")
        result = json.loads(self.run(["docker", "exec", candidates[0], "python", "-m", "scripts.analytics_status"]))
        print(json.dumps(result, ensure_ascii=False), flush=True)

    def check_collection_candidate(self):
        """Verify new read paths before touching the public overlay or writers."""
        control = self.read("/api/v1/collection-management/control", port=18081, timeout=15)
        if not (isinstance(control, dict) and type(control.get("paused")) is bool
                and type(control.get("writes_enabled")) is bool
                and isinstance(control.get("platform"), str) and isinstance(control.get("region"), str)
                and isinstance(control.get("supported_task_types"), list)
                and isinstance(control.get("enabled_task_types"), list)
                and isinstance(control.get("limits"), dict)):
            raise ValueError("Candidate collection control response is invalid")
        for resource in ("plans", "audit", "launches"):
            page = self.read(f"/api/v1/collection-management/{resource}?limit=1", port=18081, timeout=15)
            if not (isinstance(page, dict) and isinstance(page.get("items"), list)
                    and "next_before_id" in page and (page["next_before_id"] is None
                    or isinstance(page["next_before_id"], str))):
                raise ValueError(f"Candidate collection {resource} response is invalid")
        resources = self.read("/api/v1/collection-management/resources?task_type=shop_profile&limit=1", port=18081, timeout=15)
        if not (isinstance(resources, dict) and isinstance(resources.get("items"), list)
                and "next_before_id" in resources
                and all(isinstance(row, dict) and isinstance(row.get("id"), str)
                        and type(row.get("eligible")) is bool for row in resources["items"])):
            raise ValueError("Candidate collection resources response is invalid")
        monitor = self.read("/api/v1/collections/monitor?window_minutes=60", port=18081, timeout=15)
        if not (isinstance(monitor, dict) and isinstance(monitor.get("generated_at"), str)
                and all(isinstance(monitor.get(key), dict) for key in ("scope", "window", "queue", "attempts", "leases", "runtime"))
                and all(isinstance(monitor.get(key), list) for key in ("trend", "errors", "recent_failures", "alerts"))):
            raise ValueError("Candidate collection monitor response is invalid")
        if not (monitor["window"].get("minutes") == 60
                and monitor["scope"] == {key: control[key] for key in ("platform", "region")}
                and monitor["queue"].get("status") in {"fresh", "stale", "pending"}
                and isinstance(monitor.get("statistics"), dict)
                and monitor.get("task_group") == "source"
                and monitor["statistics"].get("method") == "minute_rollup"
                and ((monitor["statistics"].get("ready") is True and type(monitor["attempts"].get("total")) is int)
                     or (monitor["statistics"].get("ready") is False and monitor["attempts"].get("total") is None
                         and monitor["statistics"].get("status") == "warming"))
                and isinstance(monitor["leases"].get("items"), list)
                and monitor["runtime"].get("status") in {"available", "unavailable", "not_configured"}
                and isinstance(monitor["runtime"].get("components"), list)):
            raise ValueError("Candidate collection monitor response is invalid")
        incidents = self.read("/api/v1/collections/alerts?status=active&limit=1", port=18081, timeout=15)
        if not (isinstance(incidents, dict) and isinstance(incidents.get("items"), list)
                and "next_before_id" in incidents and isinstance(incidents.get("evaluation"), dict)
                and incidents["evaluation"].get("status") in {"never", "fresh", "stale"}):
            raise ValueError("Candidate collection alerts response is invalid")
        # Existing writers still use the previous image here. Absence of the new
        # heartbeat is expected until cutover; this check never creates work.

    def reconcile_collection_monitor(self):
        """Establish coverage after every attempt writer uses the outbox hook."""
        roles = {name: self.containers(name) for name in SERVICES}
        if len(roles["api"]) != 1 or len(roles["worker"]) != 2 or len(roles["scheduler"]) != 1:
            raise ValueError("Collection writers must have the expected process counts")
        for name, items in roles.items():
            if not upgrade_role_ready(name, items) or any(item["Config"]["Image"] != self.image for item in items):
                raise ValueError("Every collection writer must use the committed image before reconciliation")
        result = json.loads(self.run(["docker", "exec", roles["api"][0]["Id"], "python", "-m",
            "scripts.collection_monitor_rollups", "--reconcile"], timeout=90))
        if not result.get("activated"):
            raise ValueError("Collection monitor reconciliation was not activated")

    def cutover(self):
        if self.record.get("phase") not in {"warming", "live"}:
            raise ValueError("Prepare and warm the release first")
        # A completed worker-only release may legitimately differ from the API.
        # Capture it in upgrade/rollback, then reject changes after that capture.
        for name in SERVICES:
            items = self.containers(name)
            allowed = {*self.record["previous_images"].get(name, []), self.image}
            if (len(items) != (2 if name == "worker" else 1)
                    or any(item["Config"]["Image"] not in allowed for item in items) or not upgrade_role_ready(name, items)):
                raise ValueError("An application role was changed by another release: " + name)
        if not self.read("/api/v2/ui/bootstrap", port=18081)["ready"]:
            raise ValueError("Projection backfill is not ready")
        if self.record.get("previous_public_overlay"):
            analysis = self.containers("analytics")[0]
            status = json.loads(self.run(["docker", "exec", analysis["Id"], "python", "-m", "scripts.analytics_status"]))
            if not status.get("rollup_backfill", {}).get("ready"):
                raise ValueError("Incremental summary backfill is not ready")
        for path in ("/api/v2/catalog/products?page_size=2", "/api/v2/catalog/stats", "/api/v2/analytics/overview?lookback_hours=24",
                     "/api/v2/analytics/workbench?lookback_hours=24", "/api/v2/events?limit=2", "/api/v2/reports"):
            result = self.read(path, port=18081, timeout=15)
            if result.get("_meta", {}).get("status") == "pending":
                raise ValueError("Default statistics are not ready")
        self.check_collection_candidate()
        self.write_overlay(public=True)
        try:
            self.up("worker", "scheduler", "collection", "api", *ANALYTICS_SERVICES)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    if self.read("/api/v2/ui/bootstrap")["ready"]:
                        break
                except Exception:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("Public API readiness failed")
            self.reconcile_collection_monitor()
            # Reuse the prefix-aware publisher from this same committed source.
            import importlib.util
            spec = importlib.util.spec_from_file_location("committed_ui_publisher", self.source / "deployment/rollout_async.py")
            publisher = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(publisher)
            publisher.ROOT = self.root
            publisher.publish_ui(self.source, self.commit)
            # Stop only the validated staging service; retain its container for diagnosis.
            try:
                self.run([*self.compose, "stop", "api-candidate"])
            except RuntimeError:
                print(json.dumps({"phase": "candidate_cleanup_pending"}), flush=True)
            self.save("live", public_api_changed=True, analytics_concurrency=1, public_url="http://34.20.217.151/shopscope/")
            active = {"commit": self.commit, "image": self.image,
                "compose_files": [str(path) for path in self.base_files] + [str(self.overlay)],
                "compose_environment": {"APP_IMAGE": self.environment["APP_IMAGE"]},
                "worker_count": 2, "infrastructure_recreated": False, "release_record": str(self.record_path)}
            (self.root / "deployment/active-read-model-release.json").write_text(json.dumps(active, indent=2) + "\n")
        except Exception:
            self.write_overlay(public=False)
            previous_overlay = self.record.get("previous_public_overlay")
            rollback = self.base
            if previous_overlay:
                previous_overlay = Path(previous_overlay).resolve()
                if not previous_overlay.is_relative_to(self.evidence) or not previous_overlay.is_file():
                    raise ValueError("Previous public API override is outside the release record")
                rollback = [*self.base, "-f", str(previous_overlay)]
            previous_services = [name for name in (*SERVICES, *ANALYTICS_SERVICES) if name in self.record["previous_images"]]
            self.up(*previous_services, compose=rollback)
            self.save("cutover_failed_old_application_restored")
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--phase", choices=("prepare", "upgrade", "status", "cutover"), required=True)
    args = parser.parse_args()
    release = Release(args.project_root, args.release_id, args.commit)
    if args.phase == "status":
        release.status()
    else:
        import fcntl
        with (release.root / "data/samples/deployment/async-rollout.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            getattr(release, args.phase)()


if __name__ == "__main__":
    main()
