"""Publishing must not expose a deployment address or loosen its target guard."""

import subprocess
from unittest.mock import Mock

import pytest

from deployment import ssh_proxy_bridge as bridge


@pytest.fixture
def ssh_proxy(monkeypatch):
    config = Mock(return_value=subprocess.CompletedProcess(
        ["ssh", "-G", "google-vps"], 0,
        stdout="hostname monitor.example.com\nport 22\n",
    ))
    connection = Mock()
    connection.recv.side_effect = [bytes([value]) for value in b"HTTP/1.1 200 OK\r\n\r\n"]
    dial = Mock(return_value=connection)
    monkeypatch.setattr(bridge.subprocess, "run", config)
    monkeypatch.setattr(bridge.socket, "create_connection", dial)
    return config, dial, connection


def test_only_configured_ssh_target_can_use_loopback_proxy(ssh_proxy):
    config, dial, connection = ssh_proxy
    assert bridge.connect_tunnel("monitor.example.com", 22, 7897) is connection
    config.assert_called_once_with(["ssh", "-G", "google-vps"], check=True,
                                   capture_output=True, text=True, timeout=10)
    dial.assert_called_once_with(("127.0.0.1", 7897), timeout=10)
    connection.sendall.assert_called_once_with(
        b"CONNECT monitor.example.com:22 HTTP/1.1\r\nHost: monitor.example.com:22\r\n\r\n"
    )
    connection.settimeout.assert_called_once_with(None)


@pytest.mark.parametrize("host,port", [("other.example.com", 22), ("monitor.example.com", 443)])
def test_unapproved_destination_is_rejected_before_proxy_dial(ssh_proxy, host, port):
    _, dial, _ = ssh_proxy
    with pytest.raises(ValueError):
        bridge.connect_tunnel(host, port, 7897)
    dial.assert_not_called()


@pytest.mark.parametrize("stdout", ["", "hostname monitor.example.com\nport 2222\n"])
def test_missing_or_incompatible_alias_fails_closed(ssh_proxy, stdout):
    config, dial, _ = ssh_proxy
    config.return_value.stdout = stdout
    with pytest.raises(ValueError):
        bridge.connect_tunnel("monitor.example.com", 22, 7897)
    dial.assert_not_called()


def test_ssh_configuration_failure_never_dials_proxy(ssh_proxy):
    config, dial, _ = ssh_proxy
    config.side_effect = subprocess.CalledProcessError(1, ["ssh", "-G", "google-vps"])
    with pytest.raises(subprocess.CalledProcessError):
        bridge.connect_tunnel("monitor.example.com", 22, 7897)
    dial.assert_not_called()
