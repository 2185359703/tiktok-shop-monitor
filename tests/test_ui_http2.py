import pytest

from deployment.enable_ui_http2 import enable


def test_enable_keeps_the_existing_tls_and_ipv4_ipv6_listeners():
    source = 'server {\n    listen 80;\n    server_name shopscope-monitor.top www.shopscope-monitor.top;\n}\nserver {\n    listen 443 ssl;\n    listen [::]:443 ssl;\n    server_name shopscope-monitor.top www.shopscope-monitor.top;\n    ssl_certificate /etc/certs/public.pem;\n}\n'
    updated = enable(source, "shopscope-monitor.top")
    assert "listen 443 ssl http2;" in updated and "listen [::]:443 ssl http2;" in updated
    assert "listen 80;" in updated and "ssl_certificate /etc/certs/public.pem;" in updated
    assert enable(updated, "shopscope-monitor.top") == updated


def test_refuse_a_shared_site_file_with_another_application():
    with pytest.raises(ValueError, match="another application"):
        enable('server {\n listen 443 ssl;\n server_name unrelated.example;\n}\n', "shopscope-monitor.top")
