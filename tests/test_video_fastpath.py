"""Fast-path checks that do not need AstrBot or a WeChat account.

Run with ``python tests/test_video_fastpath.py`` (requires ffmpeg/ffprobe).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from video import (  # noqa: E402
    VideoInfo,
    VideoPolicy,
    engine_compatible,
    normalize_video,
    normalize_video_path,
    set_plugin_config,
)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def make_video(
    directory: Path,
    name: str,
    *,
    size: str,
    rate: int,
    seconds: int,
    codec: str = "libx264",
    extra: list[str] | None = None,
) -> Path:
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
        f"testsrc2=size={size}:rate={rate}:duration={seconds}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-c:v",
        codec,
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        *(extra or []),
        str(output),
    ]
    subprocess.run(command, check=True)
    return output


def probe_json(path: Path) -> dict:
    completed = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    import json

    return json.loads(completed.stdout)


def mov_family_ok(data: bytes) -> bool:
    """Mirror native_client.validate_video_bytes without importing AstrBot."""
    offset = boxes = 0
    movie = media = False
    while offset < len(data):
        boxes += 1
        if boxes > 65536 or len(data) - offset < 8:
            return False
        size = int.from_bytes(data[offset : offset + 4], "big")
        header = 8
        if size == 0:
            size = len(data) - offset
        elif size == 1:
            if len(data) - offset < 16:
                return False
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header = 16
        if size < header or size > len(data) - offset:
            return False
        kind = data[offset + 4 : offset + 8]
        if kind == b"moov":
            movie = size > header
        elif kind == b"mdat":
            media |= size > header
        offset += size
    return movie and media


def speed_policy() -> VideoPolicy:
    return replace(
        VideoPolicy.load(),
        transcode=True,
        max_edge=1280,
        max_fps=30.0,
        crf=23,
        max_output_bytes=24 * 1024 * 1024,
    )


def quality_transcode_policy() -> VideoPolicy:
    return replace(VideoPolicy.load(), transcode=True)


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg/ffprobe not installed")
class VideoFastPathTests(unittest.TestCase):
    def test_default_policy_never_reencodes(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            source = make_video(
                directory,
                "hevc.mp4",
                size="1280x720",
                rate=60,
                seconds=2,
                codec="libx265",
                extra=["-tag:v", "hvc1"],
            )
            original = source.read_bytes()
            result, content_type = asyncio.run(
                normalize_video(original, "video/mp4", VideoPolicy.load())
            )
            self.assertIs(result, original)
            self.assertEqual(content_type, "video/mp4")

    def test_quality_transcode_keeps_resolution_and_frame_rate(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            source = make_video(
                directory,
                "source.mp4",
                size="1920x1080",
                rate=60,
                seconds=2,
                codec="libx265",
                extra=["-tag:v", "hvc1"],
            )
            normalized, _ = asyncio.run(
                normalize_video(
                    source.read_bytes(), "video/mp4", quality_transcode_policy()
                )
            )
            self.assertTrue(mov_family_ok(normalized))
            output = directory / "normalized.mp4"
            output.write_bytes(normalized)
            video = next(
                stream
                for stream in probe_json(output)["streams"]
                if stream["codec_type"] == "video"
            )
            self.assertEqual(video["codec_name"], "h264")
            self.assertEqual((video["width"], video["height"]), (1920, 1080))
            numerator, denominator = video["avg_frame_rate"].split("/")
            self.assertAlmostEqual(float(numerator) / float(denominator), 60.0, 1)

    def test_path_fastpath_matches_bytes_fastpath(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            source = make_video(
                directory,
                "source.mp4",
                size="1920x1080",
                rate=60,
                seconds=2,
                codec="libx265",
                extra=["-tag:v", "hvc1"],
            )
            normalized = asyncio.run(
                normalize_video_path(source, "video/mp4", quality_transcode_policy())
            )
            self.assertIsNotNone(normalized)
            data, content_type = normalized
            self.assertEqual(content_type, "video/mp4")
            self.assertTrue(mov_family_ok(data))
            output = directory / "path-normalized.mp4"
            output.write_bytes(data)
            video = next(
                stream
                for stream in probe_json(output)["streams"]
                if stream["codec_type"] == "video"
            )
            self.assertEqual((video["width"], video["height"]), (1920, 1080))

    def test_speed_profile_downscales_and_caps_to_30fps(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            source = make_video(
                directory, "source.mp4", size="1920x1080", rate=60, seconds=2
            )
            normalized, content_type = asyncio.run(
                normalize_video(source.read_bytes(), "video/mp4", speed_policy())
            )
            self.assertEqual(content_type, "video/mp4")
            self.assertTrue(mov_family_ok(normalized))
            self.assertLess(len(normalized), source.stat().st_size)
            output = directory / "normalized.mp4"
            output.write_bytes(normalized)
            report = probe_json(output)
            video = next(
                stream
                for stream in report["streams"]
                if stream["codec_type"] == "video"
            )
            self.assertEqual(video["codec_name"], "h264")
            self.assertEqual(video["pix_fmt"], "yuv420p")
            self.assertLessEqual(max(video["width"], video["height"]), 1280)
            numerator, denominator = video["avg_frame_rate"].split("/")
            self.assertLessEqual(float(numerator) / float(denominator), 30.01)

    def test_compatible_mp4_is_passed_through_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            source = make_video(
                directory, "compatible.mp4", size="640x360", rate=25, seconds=2
            )
            original = source.read_bytes()
            normalized, content_type = asyncio.run(
                normalize_video(
                    original, "video/mp4", quality_transcode_policy()
                )
            )
            self.assertEqual(content_type, "video/mp4")
            self.assertIs(normalized, original)

    def test_hevc_source_is_transcoded(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            source = make_video(
                directory,
                "hevc.mp4",
                size="1280x720",
                rate=30,
                seconds=2,
                codec="libx265",
                extra=["-tag:v", "hvc1"],
            )
            normalized, _ = asyncio.run(
                normalize_video(
                    source.read_bytes(), "video/mp4", quality_transcode_policy()
                )
            )
            self.assertNotEqual(normalized, source.read_bytes())
            self.assertTrue(mov_family_ok(normalized))
            output = directory / "normalized.mp4"
            output.write_bytes(normalized)
            video = next(
                stream
                for stream in probe_json(output)["streams"]
                if stream["codec_type"] == "video"
            )
            self.assertEqual(video["codec_name"], "h264")

    def test_missing_ffmpeg_returns_original(self):
        original = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
        policy = replace(
            VideoPolicy.load(), transcode=True, ffmpeg=None, ffprobe=None
        )
        normalized, content_type = asyncio.run(
            normalize_video(original, "video/mp4", policy)
        )
        self.assertEqual(content_type, "video/mp4")
        self.assertIs(normalized, original)

    def test_unparsable_bytes_fall_back_to_original(self):
        original = b"not a video at all" * 10
        normalized, content_type = asyncio.run(
            normalize_video(original, "video/mp4", quality_transcode_policy())
        )
        self.assertEqual(content_type, "video/mp4")
        self.assertIs(normalized, original)


class CompatibilityTests(unittest.TestCase):
    def info(self, **overrides) -> VideoInfo:
        values = dict(
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            duration=5.0,
            video_codec="h264",
            profile="main",
            pix_fmt="yuv420p",
            width=1280,
            height=720,
            fps=30.0,
            rotation=0,
            sample_aspect="1:1",
            audio_codec="aac",
            audio_rate=48000,
            audio_channels=2,
        )
        values.update(overrides)
        return VideoInfo(**values)

    def test_compatible_sets(self):
        data = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
        policy = VideoPolicy()
        self.assertTrue(engine_compatible(data, self.info(), policy))

    def test_incompatible_sets(self):
        data = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
        policy = VideoPolicy()
        for overrides in (
            {"video_codec": "hevc"},
            {"pix_fmt": "yuv420p10le"},
            {"rotation": 90.0},
            {"audio_codec": "opus"},
            {"audio_rate": 22050},
            {"audio_channels": 6},
        ):
            with self.subTest(overrides=overrides):
                self.assertFalse(
                    engine_compatible(data, self.info(**overrides), policy)
                )

    def test_optional_bounds_reject_high_resolution_or_frame_rate(self):
        data = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
        bounded = VideoPolicy(max_edge=1280, max_fps=30.0, max_seconds=600.0)
        for overrides in (
            {"width": 1921},
            {"height": 721},
            {"fps": 60.0},
            {"duration": 601.0},
        ):
            with self.subTest(overrides=overrides):
                self.assertFalse(
                    engine_compatible(data, self.info(**overrides), bounded)
                )

    def test_default_policy_describes_passthrough_only(self):
        self.assertIn("passthrough-only", VideoPolicy().describe())

    def test_plugin_config_overrides_legacy_file(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / "video_settings.json").write_text(
                '{"transcode": false, "max_edge": 1280}', encoding="utf-8"
            )
            set_plugin_config(
                {"video": {"transcode": True, "max_edge": 0, "crf": 18}}
            )
            try:
                policy = VideoPolicy.load(directory)
                self.assertTrue(policy.transcode)
                self.assertEqual(policy.max_edge, 0)
                self.assertEqual(policy.crf, 18)
            finally:
                set_plugin_config(None)

    def test_flat_plugin_config_is_accepted(self):
        set_plugin_config({"transcode": True, "max_fps": 24})
        try:
            policy = VideoPolicy.load()
            self.assertTrue(policy.transcode)
            self.assertEqual(policy.max_fps, 24)
        finally:
            set_plugin_config(None)

    def test_quality_policy_allows_high_resolution_and_frame_rate(self):
        data = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
        self.assertTrue(
            engine_compatible(
                data,
                self.info(width=3840, height=2160, fps=120.0),
                VideoPolicy(),
            )
        )

    def test_quicktime_brand_is_not_passthrough(self):
        data = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 32
        self.assertFalse(engine_compatible(data, self.info(), VideoPolicy()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
