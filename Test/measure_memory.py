"""Measure the shipped Linux engine on an offline, disposable account."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request


def memory(pid):
    status = Path(f"/proc/{pid}/status").read_text()
    values = {}
    for line in status.splitlines():
        if line.startswith(("VmRSS:", "VmHWM:")):
            key, amount, _ = line.split()
            values[key[:-1]] = int(amount) / 1024
    return values


def main():
    binary = next(Path(os.environ["PROBE_ARTIFACT"]).rglob("wx"))
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == "28955089136b251513df648e48585b4912c9ed1bc67866f82a90e2484c5e6bb0"
    binary.chmod(0o700)
    report = {"unit": "MiB", "environment": "ubuntu-24.04 GitHub runner, offline synthetic account", "logged_in_measured": False, "measurements": []}
    with tempfile.TemporaryDirectory(prefix="webridge-memory-") as temporary:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        token = secrets.token_urlsafe(48)
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith(("WECHAT_", "WX_", "AGENT_WECHAT_"))}
        environment["HOME"] = temporary
        with open(Path(temporary) / "engine.log", "wb") as log:
            engine = subprocess.Popen([str(binary), "serve", "--data-dir", temporary, "--token-stdin", "--listen", f"127.0.0.1:{port}"], stdin=subprocess.PIPE, stdout=log, stderr=log, env=environment)
            engine.stdin.write((token + "\n").encode())
            engine.stdin.close()

            def request(method, path, payload=None):
                body = json.dumps(payload).encode() if payload is not None else None
                req = urllib.request.Request(f"http://127.0.0.1:{port}/v1{path}", data=body, method=method, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=120) as response:
                    return json.load(response)

            try:
                for _ in range(100):
                    try:
                        request("GET", "/status")
                        break
                    except OSError:
                        assert engine.poll() is None, "engine exited before ready"
                        time.sleep(.1)
                else:
                    raise RuntimeError("engine readiness timeout")
                time.sleep(2)
                report["measurements"].append({"phase": "empty_idle", **memory(engine.pid)})
                request("POST", "/accounts", {"id": "memory-offline"})
                time.sleep(2)
                report["measurements"].append({"phase": "one_offline_account_idle", **memory(engine.pid)})
                for size_mib in (50, 100):
                    stop = threading.Event()
                    peak = [0.0]

                    def sample():
                        while not stop.wait(.01):
                            try:
                                peak[0] = max(peak[0], memory(engine.pid)["VmRSS"])
                            except FileNotFoundError:
                                return

                    sampler = threading.Thread(target=sample)
                    sampler.start()
                    boundary = "webridge-memory-probe"
                    prefix = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="memory.mp4"\r\nContent-Type: video/mp4\r\n\r\n'.encode()
                    suffix = f'\r\n--{boundary}--\r\n'.encode()
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
                    connection.putrequest("POST", "/v1/accounts/memory-offline/uploads?temporary=true")
                    connection.putheader("Authorization", "Bearer " + token)
                    connection.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
                    connection.putheader("Content-Length", str(len(prefix) + size_mib * 1024 * 1024 + len(suffix)))
                    connection.endheaders()
                    connection.send(prefix)
                    chunk = bytes(1024 * 1024)
                    for _ in range(size_mib):
                        connection.send(chunk)
                    connection.send(suffix)
                    response = connection.getresponse()
                    result = json.loads(response.read())
                    assert response.status == 201, response.status
                    assert result["id"].startswith("upload_tmp_")
                    connection.close()
                    time.sleep(.1)
                    stop.set()
                    sampler.join()
                    report["measurements"].append({"phase": f"temporary_upload_{size_mib}MiB", "sampled_peak_rss": peak[0], **memory(engine.pid)})
                    time.sleep(3)
                    report["measurements"].append({"phase": f"after_upload_{size_mib}MiB", **memory(engine.pid)})
            finally:
                engine.send_signal(signal.SIGINT)
                try:
                    engine.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    engine.kill()
                    engine.wait()
    Path("memory-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
