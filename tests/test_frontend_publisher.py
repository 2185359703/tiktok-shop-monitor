from deployment.publish_frontend import backend_fingerprint


def test_static_publication_fingerprint_rejects_backend_changes(tmp_path):
    for name in ("Dockerfile", "requirements.txt", "pyproject.toml", "main.py", "app/main.py", "app/reports/template.html",
                 "app/frontend/app.js", "scripts/run_api.py", "configs/settings.toml", "data/seed/shops.csv"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original")
    original = backend_fingerprint(tmp_path)
    (tmp_path / "app/frontend/app.js").write_text("new UI")
    assert backend_fingerprint(tmp_path) == original
    for name in ("app/main.py", "app/reports/template.html", "scripts/run_api.py", "configs/settings.toml", "data/seed/shops.csv"):
        path = tmp_path / name
        path.write_text("changed backend")
        assert backend_fingerprint(tmp_path) != original
        path.write_text("original")
