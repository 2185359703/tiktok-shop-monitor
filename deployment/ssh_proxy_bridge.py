"""Opaque TCP bridge for this VPS's SSH through an existing loopback proxy.

OpenSSH still owns authentication and known-host verification. This helper does
not read keys, decrypt SSH, change the SSH config, or create a public listener.
"""

import argparse
import socket
import subprocess
import sys
from threading import Thread


def connect_tunnel(host, port, proxy_port):
    if port != 22:
        raise ValueError("只允许本项目已确认的 VPS SSH 目标。")
    # Resolve the same local alias as open_tunnel.ps1 without opening a connection.
    # Deployment addresses stay in the user's SSH config, outside the repository.
    result = subprocess.run(["ssh", "-G", "google-vps"], check=True,
                            capture_output=True, text=True, timeout=10)
    options = dict(line.split(None, 1) for line in result.stdout.splitlines()
                   if len(line.split(None, 1)) == 2)
    if host != options.get("hostname") or options.get("port") != "22":
        raise ValueError("只允许本项目已确认的 VPS SSH 目标。")
    connection = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    try:
        connection.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode("ascii"))
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            chunk = connection.recv(1)
            if not chunk or len(header) >= 8192:
                raise ValueError("本机代理握手未完成。")
            header.extend(chunk)
        line = bytes(header).split(b"\r\n", 1)[0].split()
        if len(line) < 2 or line[1] != b"200":
            raise ValueError("本机代理没有接受 SSH 连接。")
        connection.settimeout(None)
        return connection
    except BaseException:
        connection.close()
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("host")
    parser.add_argument("port", type=int)
    parser.add_argument("--proxy-port", type=int, required=True)
    args = parser.parse_args()
    connection = connect_tunnel(args.host, args.port, args.proxy_port)
    def upload():
        try:
            while chunk := sys.stdin.buffer.read1(65536):
                connection.sendall(chunk)
            connection.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    Thread(target=upload, daemon=True).start()
    try:
        while chunk := connection.recv(65536):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("本项目的本机代理转发未完成；未输出 SSH 数据或认证信息。", file=sys.stderr)
        raise SystemExit(1)
