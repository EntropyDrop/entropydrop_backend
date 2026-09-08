"""Exercise the shipped Nginx limits against an isolated echo upstream.

Requires Docker. No production services, data or configuration are modified.
"""
import pathlib
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid


def main():
    config = (pathlib.Path(__file__).resolve().parents[1] / "nginx.conf").read_text()
    # Preserve every shipped location; provide its upstream inside this container.
    config = config.rsplit("}", 1)[0] + '\nserver { listen 8000; client_max_body_size 20m; location / { return 204; } }\n}\n'
    name = "ed-review-nginx-" + uuid.uuid4().hex[:12]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="ed-nginx-", dir="/private/tmp") as directory:
        conf = pathlib.Path(directory) / "nginx.conf"
        conf.write_text(config)
        try:
            subprocess.run(["docker", "run", "--rm", "-d", "--name", name,
                            "--add-host", "app:127.0.0.1", "-p", f"127.0.0.1:{port}:80",
                            "--mount", f"type=bind,source={conf},target=/etc/nginx/nginx.conf,readonly",
                            "nginx:1.28-alpine"], check=True, stdout=subprocess.DEVNULL)
            for _ in range(100):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/ready", timeout=1):
                        break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.1)
            else:
                raise RuntimeError("Isolated Nginx did not start")
            subprocess.run(["docker", "exec", name, "nginx", "-t"], check=True)
            cases = [
                ("/skin/api/example", "POST", 512 * 1024, 204),
                ("/skin/api/example", "POST", 512 * 1024 + 1, 413),
                ("/space/api/v2/market/resources", "POST", 9 * 1024**2, 204),
                ("/space/api/v2/market/resources/", "POST", 9 * 1024**2 + 1, 413),
                ("/space/api/v2/worlds/test/entities", "POST", 17 * 1024**2, 204),
                ("/space/api/v2/worlds/test/entities/", "POST", 17 * 1024**2 + 1, 413),
                ("/space/api/v2/worlds/test/entities/browser", "POST", 17 * 1024**2, 204),
                ("/space/api/v2/worlds/test/entities/one/checkpoint", "PUT", 17 * 1024**2, 204),
                ("/space/api/v2/worlds/test/entities/one/checkpoint/", "PUT", 17 * 1024**2 + 1, 413),
                ("/space/api/v2/worlds/test/players", "POST", 512 * 1024 + 1, 413),
            ]
            for path, method, size, expected in cases:
                request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b"x" * size, method=method)
                try:
                    with urllib.request.urlopen(request, timeout=20) as response:
                        status = response.status
                except urllib.error.HTTPError as exc:
                    status = exc.code
                assert status == expected, (path, size, expected, status)
                print(f"PASS {method} {path} ({size} bytes): {status}", flush=True)
            print(f"{len(cases)} proxy boundary checks passed")
        finally:
            subprocess.run(["docker", "stop", "-t", "1", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
