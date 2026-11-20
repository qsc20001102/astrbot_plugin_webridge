"""File validator and default passthrough behaviour, without AstrBot installed."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path

# Stub the two AstrBot attributes native_client imports at module load.
astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


api.logger = _Logger()
astrbot.api = api
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import astrbot_plugin_webridge.native_client as native_client  # noqa: E402
from astrbot_plugin_webridge.video import VideoPolicy  # noqa: E402

FFMPEG = shutil.which("ffmpeg")


def make_video(directory: Path, name: str, *, codec: str, muxer: str = "mp4") -> Path:
    output = directory / name
    command = [
        FFMPEG,
        "-hide_banner",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=10:duration=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=1",
        "-c:v",
        codec,
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-f",
        muxer,
        str(output),
    ]
    if codec == "libx265":
        command[-3:-3] = ["-tag:v", "hvc1"]
    subprocess.run(command, check=True)
    return output


class FakeVideo:
    def __init__(self, source: str):
        self.source = source

    async def _resolve_file_source(self):
        return self.source


class AcceptedMediaActionTests(unittest.TestCase):
    def action(self, value, kind="video", chunks=None):
        return native_client._accepted_media_action(value, kind, chunks or [])

    def test_complete_requires_server_id_and_one_message_id(self):
        value = {
            "serverMessageId": "server-1",
            "result": {"profileProjection": "complete", "messageIds": ["local-1"]},
        }
        self.assertEqual(self.action(value), "complete")

    def test_pending_projection_with_server_id_is_accepted(self):
        value = {
            "serverMessageId": "server-1",
            "result": {"profileProjection": "pending", "messageIds": []},
        }
        self.assertEqual(self.action(value), "accepted")

    def test_not_applicable_projection_with_server_id_is_accepted(self):
        value = {
            "serverMessageId": "server-1",
            "result": {"profileProjection": "not_applicable"},
        }
        self.assertEqual(self.action(value), "accepted")

    def test_missing_server_id_waits(self):
        value = {"result": {"profileProjection": "pending"}}
        self.assertEqual(self.action(value), "wait")

    def test_unknown_projection_is_reviewed(self):
        value = {
            "serverMessageId": "server-1",
            "result": {"profileProjection": "unknown", "messageIds": ["local-1"]},
        }
        self.assertEqual(self.action(value), "review")

    def test_incomplete_voice_waits(self):
        chunks = [
            {
                "index": 0,
                "state": "accepted",
                "durationMs": 1000,
                "serverMessageId": "server-1",
            }
        ]
        value = {"serverMessageId": "server-1", "result": {"messageIds": []}}
        self.assertEqual(self.action(value, kind="voice", chunks=chunks), "wait")


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class FileValidationTests(unittest.TestCase):
    def test_file_and_bytes_validators_agree(self):
        cases = [
            ("sample.mp4", "libx264", "mp4"),
            ("sample.mov", "libx264", "mov"),
            ("sample.3gp", "mpeg4", "3gp"),
        ]
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            for name, codec, muxer in cases:
                with self.subTest(name=name):
                    path = make_video(directory, name, codec=codec, muxer=muxer)
                    from_file = native_client.validate_video_file(path)
                    from_bytes = native_client.validate_video_bytes(path.read_bytes())
                    self.assertEqual(from_file, from_bytes)

    def test_truncated_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            path = make_video(directory, "sample.mp4", codec="libx264")
            truncated = directory / "truncated.mp4"
            data = path.read_bytes()
            truncated.write_bytes(data[: len(data) // 2])
            with self.assertRaises(native_client.MediaError):
                native_client.validate_video_file(truncated)

    def test_local_path_detection(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "sample.mp4"
            path.write_bytes(b"\x00" * 16)
            self.assertEqual(native_client._local_media_path(str(path)), path)
            self.assertIsNone(native_client._local_media_path("https://x/y.mp4"))
            self.assertIsNone(native_client._local_media_path("base64://AAAA"))
            self.assertIsNone(native_client._local_media_path("data:video/mp4;base64,AA"))
            with self.assertRaises(ValueError):
                native_client._local_media_path("\\\\server\\share\\a.mp4")


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class DefaultPassthroughTests(unittest.TestCase):
    def test_default_policy_returns_source_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            source = make_video(directory, "hevc.mp4", codec="libx265")
            original = source.read_bytes()
            data, content_type = asyncio.run(
                native_client.load_outgoing_video(
                    FakeVideo(str(source)), policy=VideoPolicy.load()
                )
            )
            self.assertEqual(content_type, "video/mp4")
            self.assertEqual(data, original)

    def test_explicit_transcode_changes_hevc(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            source = make_video(directory, "hevc.mp4", codec="libx265")
            policy = replace(VideoPolicy.load(), transcode=True)
            data, content_type = asyncio.run(
                native_client.load_outgoing_video(
                    FakeVideo(str(source)), policy=policy
                )
            )
            self.assertEqual(content_type, "video/mp4")
            self.assertNotEqual(data, source.read_bytes())
            self.assertEqual(native_client.validate_video_bytes(data), "video/mp4")


if __name__ == "__main__":
    unittest.main(verbosity=2)
