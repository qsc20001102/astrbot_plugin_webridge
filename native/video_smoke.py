"""Stage a large video in a fresh, offline Native account; never submit a send."""

import asyncio
import hashlib
import importlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]


async def main():
    if os.environ.get("WEBRIDGE_OFFLINE_VIDEO_SMOKE") != "1" or any(
        name != "lo" for _, name in socket.if_nameindex()
    ):
        raise RuntimeError("Run only in an isolated container with --network none")
    package = types.ModuleType("webridge_video_smoke")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    runtime = importlib.import_module("webridge_video_smoke.runtime")
    config = importlib.import_module("webridge_video_smoke.config")
    with tempfile.TemporaryDirectory(prefix="webridge-video-contract-") as temporary:
        directory = Path(temporary)
        engine = runtime.NativeRuntime(directory / "engine")
        try:
            await engine.start()
            await engine.client._request("POST", "/accounts", json={"id": "offline-video"})
            path = directory / "source.mp4"
            await asyncio.to_thread(
                subprocess.run,
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=160x120:rate=5:duration=1",
                    "-c:v",
                    "libx264",
                    "-threads",
                    "1",
                    "-pix_fmt",
                    "yuv420p",
                    str(path),
                ],
                check=True,
                capture_output=True,
            )
            # Keep a real decodable MP4, enlarged past the former source cap.
            with path.open("ab") as stream:
                padding = 51 * 1024 * 1024
                stream.write((padding + 8).to_bytes(4, "big") + b"free")
                for _ in range(51):
                    stream.write(bytes(1024 * 1024))
            size = path.stat().st_size
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            async with await engine.account_client("offline-video") as client:
                caps = await client.capabilities()
                assert caps["mediaSendMaxBytes"]["video"] == config.VIDEO_PROTOCOL_MAX_BYTES
                assert caps["mediaUploadMaxBytes"] >= config.VIDEO_PROTOCOL_MAX_BYTES
                with path.open("rb") as stream:
                    form = aiohttp.FormData()
                    form.add_field("file", stream, filename="source.mp4", content_type="video/mp4")
                    uploaded = await client._request(
                        "POST",
                        client.prefix + "/uploads",
                        data=form,
                        timeout=aiohttp.ClientTimeout(total=180),
                    )
                assert uploaded["byteLength"] == size > 50 * 1024 * 1024
                assert uploaded["sha256"] == digest
                assert (await client.account())["authenticated"] is False
            print(
                json.dumps(
                    {
                        "large_video_staging": "passed",
                        "source_bytes": size,
                        "source_sha256": digest,
                        "real_native_multipart_and_storage": True,
                        "network_isolated": True,
                        "wechat_login_or_send": False,
                    }
                )
            )
        finally:
            await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
