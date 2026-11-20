"""Outgoing video quality policy.

The bundled Native engine has its own preparation pipeline (ffmpeg, CRF 18,
source resolution, two threads) and uploads the playback copy to the WeChat
CDN in 64 KiB sequential chunks. The fastest safe win on the Python side is to
never add a second lossy generation:

* by default (``transcode=False``) this module is a strict no-op; compatible
  MP4 bytes are handed to the engine untouched and everything else is left to
  Native, exactly as before;
* ``transcode=True`` is an explicit opt-in. Its defaults keep the source
  resolution, frame rate and audio channel count and use CRF 18, so it is
  useful for converting HEVC/other containers on a faster host without
  downscaling;
* a speed profile (1280 long edge, 30 fps, CRF 23) is still available but must
  be configured explicitly.

Settings are read from ``video_settings.json`` in the plugin data directory
and can be overridden per deployment with ``WEBRIDGE_VIDEO_*`` environment
variables. On any failure the original bytes are returned.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

try:  # The module is also importable from standalone tests without AstrBot.
    from astrbot.api import logger
except ImportError:  # pragma: no cover - exercised by the local test runner
    import logging

    logger = logging.getLogger("webridge.video")

MiB = 1024 * 1024
_VIDEO_CONTENT_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/3gpp",
    "video/3gpp2",
    "video/mj2",
}
_ENGINE_PROFILES = {"constrained baseline", "baseline", "main", "high"}
_ENGINE_SAMPLE_RATES = {44100, 48000}
_X264_PRESETS = {
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
}
_ENCODER_SLOT = asyncio.Semaphore(1)

FILE_SETTINGS_NAME = "video_settings.json"

_POLICY_KEYS = {
    "enabled",
    "transcode",
    "max_edge",
    "max_fps",
    "crf",
    "preset",
    "audio_kbps",
    "timeout",
    "max_seconds",
    "max_output_mb",
    "max_passthrough_mb",
    "max_input_mb",
}
# Filled by WeBridgePlugin.__init__ from AstrBot's plugin config (schema).
_PLUGIN_CONFIG: dict[str, object] = {}


def set_plugin_config(config) -> None:
    """Receive the AstrBot plugin config; the ``video`` object wins over files."""
    global _PLUGIN_CONFIG
    if not isinstance(config, dict):
        _PLUGIN_CONFIG = {}
        return
    nested = config.get("video")
    if isinstance(nested, dict):
        _PLUGIN_CONFIG = dict(nested)
    else:
        _PLUGIN_CONFIG = {
            key: config[key] for key in _POLICY_KEYS if key in config
        }


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() not in {"0", "false", "no", "off", "never", "disabled"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        logger.warning("视频设置 %s=%r 不是整数，使用默认值 %s", name, value, default)
        return default
    return max(minimum, min(maximum, parsed))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    value = _env(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        logger.warning("视频设置 %s=%r 不是数字，使用默认值 %s", name, value, default)
        return default
    return max(minimum, min(maximum, parsed))


@dataclass(frozen=True)
class VideoPolicy:
    """Tuning for the optional local normalization step.

    By default (``transcode=False``) Python never re-encodes: compatible MP4
    bytes are handed to the engine untouched and everything else is left to
    Native's own preparation. Enabling ``transcode`` is an explicit opt-in;
    the defaults then keep the source resolution and frame rate and use CRF 18.
    """

    enabled: bool = True
    transcode: bool = False
    max_edge: int = 0
    max_fps: float = 0.0
    crf: int = 18
    preset: str = "veryfast"
    audio_kbps: int = 128
    timeout: float = 120.0
    max_seconds: float = 3600.0
    max_output_bytes: int = 0
    max_passthrough_bytes: int = 4096 * MiB
    max_input_bytes: int = 512 * MiB
    ffmpeg: str | None = None
    ffprobe: str | None = None

    @classmethod
    def load(cls, data_dir: Path | None = None) -> "VideoPolicy":
        values: dict[str, object] = {}
        if data_dir is not None:
            path = Path(data_dir) / FILE_SETTINGS_NAME
            try:
                if path.is_file():
                    parsed = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(parsed, dict):
                        values = parsed
                    else:
                        logger.warning("视频设置文件不是 JSON 对象，已忽略：%s", path)
            except (OSError, ValueError) as error:
                logger.warning("视频设置文件读取失败，已忽略：%s (%s)", path, error)
        # AstrBot's plugin config is the primary source; the legacy JSON file
        # only fills values that are not present there.
        for key in _POLICY_KEYS:
            if key in _PLUGIN_CONFIG:
                values[key] = _PLUGIN_CONFIG[key]
        file_preset = str(values.get("preset", cls.preset))
        if file_preset.lower() not in _X264_PRESETS:
            file_preset = cls.preset
        policy = replace(
            cls(),
            enabled=_coerce_bool(values.get("enabled"), cls.enabled),
            transcode=_coerce_bool(values.get("transcode"), cls.transcode),
            max_edge=_coerce_int(values.get("max_edge"), cls.max_edge, 0, 8192),
            max_fps=_coerce_float(values.get("max_fps"), cls.max_fps, 0.0, 240.0),
            crf=_coerce_int(values.get("crf"), cls.crf, 0, 51),
            preset=file_preset,
            audio_kbps=_coerce_int(values.get("audio_kbps"), cls.audio_kbps, 24, 320),
            timeout=_coerce_float(values.get("timeout"), cls.timeout, 10.0, 1800.0),
            max_seconds=_coerce_float(values.get("max_seconds"), cls.max_seconds, 1.0, 3600.0),
            max_output_bytes=_coerce_int(
                values.get("max_output_mb"), cls.max_output_bytes // MiB, 0, 8192
            )
            * MiB,
            max_passthrough_bytes=_coerce_int(
                values.get("max_passthrough_mb"), cls.max_passthrough_bytes // MiB, 0, 8192
            )
            * MiB,
            max_input_bytes=_coerce_int(
                values.get("max_input_mb"), cls.max_input_bytes // MiB, 1, 8192
            )
            * MiB,
        )
        env_preset = _env("WEBRIDGE_VIDEO_PRESET")
        if env_preset is not None and env_preset.lower() not in _X264_PRESETS:
            logger.warning("视频设置 WEBRIDGE_VIDEO_PRESET=%r 无效，已忽略", env_preset)
            env_preset = None
        policy = replace(
            policy,
            enabled=_env_bool("WEBRIDGE_VIDEO_FASTPATH", policy.enabled),
            transcode=_env_bool("WEBRIDGE_VIDEO_TRANSCODE", policy.transcode),
            max_edge=_env_int("WEBRIDGE_VIDEO_MAX_EDGE", policy.max_edge, 0, 8192),
            max_fps=_env_float("WEBRIDGE_VIDEO_MAX_FPS", policy.max_fps, 0.0, 240.0),
            crf=_env_int("WEBRIDGE_VIDEO_CRF", policy.crf, 0, 51),
            preset=env_preset or policy.preset,
            audio_kbps=_env_int("WEBRIDGE_VIDEO_AUDIO_KBPS", policy.audio_kbps, 24, 320),
            timeout=_env_float("WEBRIDGE_VIDEO_TIMEOUT", policy.timeout, 10.0, 1800.0),
            max_seconds=_env_float("WEBRIDGE_VIDEO_MAX_SECONDS", policy.max_seconds, 1.0, 3600.0),
            max_output_bytes=_env_int(
                "WEBRIDGE_VIDEO_MAX_OUTPUT_MB", policy.max_output_bytes // MiB, 0, 8192
            )
            * MiB,
            max_passthrough_bytes=_env_int(
                "WEBRIDGE_VIDEO_PASSTHROUGH_MB",
                policy.max_passthrough_bytes // MiB,
                0,
                8192,
            )
            * MiB,
            max_input_bytes=_env_int(
                "WEBRIDGE_VIDEO_MAX_INPUT_MB", policy.max_input_bytes // MiB, 1, 8192
            )
            * MiB,
        )
        return replace(
            policy,
            ffmpeg=shutil.which("ffmpeg"),
            ffprobe=shutil.which("ffprobe"),
        )

    def describe(self) -> str:
        if not self.enabled:
            return "off"
        if not self.transcode:
            return "passthrough-only (no local re-encode)"
        if not self.ffmpeg or not self.ffprobe:
            return "unavailable (ffmpeg/ffprobe not found)"
        edge = "source" if self.max_edge <= 0 else str(self.max_edge)
        fps = "source" if self.max_fps <= 0 else f"{self.max_fps:g}"
        cap = "none" if self.max_output_bytes <= 0 else _human_bytes(self.max_output_bytes)
        return (
            f"transcode edge={edge} fps={fps} crf={self.crf} preset={self.preset} "
            f"audio={self.audio_kbps}k cap={cap} timeout={self.timeout:g}s"
        )


def _coerce_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "never", "disabled"}
    return default


def _coerce_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _coerce_float(value, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


@dataclass(frozen=True)
class VideoInfo:
    format_name: str
    duration: float
    video_codec: str
    profile: str
    pix_fmt: str
    width: int
    height: int
    fps: float
    rotation: float
    sample_aspect: str
    audio_codec: str | None
    audio_rate: int | None
    audio_channels: int | None


class VideoEncodeError(RuntimeError):
    pass


class VideoEncodeTimeout(VideoEncodeError):
    pass


def _fraction(value: object) -> float:
    if not isinstance(value, str) or "/" not in value:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0
    numerator, _, denominator = value.partition("/")
    try:
        top, bottom = float(numerator), float(denominator)
    except ValueError:
        return 0.0
    return top / bottom if bottom else 0.0


def _rotation(stream: dict) -> float:
    rotate = stream.get("tags", {}).get("rotate") if isinstance(stream.get("tags"), dict) else None
    if rotate is not None:
        try:
            return abs(float(rotate)) % 360
        except (TypeError, ValueError):
            pass
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, dict) and "rotation" in side_data:
            try:
                return abs(float(side_data["rotation"])) % 360
            except (TypeError, ValueError):
                pass
    return 0.0


def _parse_probe(report: dict) -> VideoInfo:
    streams = report.get("streams")
    if not isinstance(streams, list):
        raise VideoEncodeError("ffprobe 输出缺少 streams")
    videos = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"]
    audios = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise VideoEncodeError(f"视频流数量异常：{len(videos)}")
    video = videos[0]
    audio = audios[0] if audios else None
    fmt = report.get("format") if isinstance(report.get("format"), dict) else {}
    duration = _fraction(fmt.get("duration")) or _fraction(video.get("duration"))
    if duration <= 0:
        raise VideoEncodeError("无法读取视频时长")
    return VideoInfo(
        format_name=str(fmt.get("format_name") or ""),
        duration=duration,
        video_codec=str(video.get("codec_name") or ""),
        profile=str(video.get("profile") or "").lower(),
        pix_fmt=str(video.get("pix_fmt") or ""),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=_fraction(video.get("avg_frame_rate")),
        rotation=_rotation(video),
        sample_aspect=str(video.get("sample_aspect_ratio") or ""),
        audio_codec=str(audio.get("codec_name") or "") if audio else None,
        audio_rate=int(audio.get("sample_rate") or 0) if audio else None,
        audio_channels=int(audio.get("channels") or 0) if audio else None,
    )


async def _read_bounded(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    if stream is None:
        return b""
    data = bytearray()
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return bytes(data)
        if len(data) < limit:
            data.extend(chunk[: limit - len(data)])


async def _probe_file(path: Path, policy: VideoPolicy) -> VideoInfo:
    if not policy.ffprobe:
        raise VideoEncodeError("找不到 ffprobe")
    command = [
        policy.ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-i",
        str(path),
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    stdout_task = asyncio.create_task(_read_bounded(process.stdout, 2 * MiB))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, 8192))
    try:
        async with asyncio.timeout(min(30.0, policy.timeout)):
            await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise VideoEncodeTimeout("ffprobe 超时") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    stdout = await stdout_task
    stderr = await stderr_task
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip()[:400]
        raise VideoEncodeError(f"ffprobe 失败：{detail or process.returncode}")
    try:
        return _parse_probe(json.loads(stdout))
    except (ValueError, KeyError, TypeError) as error:
        raise VideoEncodeError(f"ffprobe 输出无法解析：{error}") from None


def _is_mp4(data: bytes) -> bool:
    if len(data) < 12 or data[4:8] != b"ftyp":
        return False
    brand = data[8:12]
    return not (brand == b"qt  " or brand.startswith(b"3g") or brand in {b"mjp2", b"mj2s"})


def _is_mp4_file(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            header = stream.read(12)
    except OSError:
        return False
    return _is_mp4(header)


def _even(value: int) -> bool:
    return value > 0 and value % 2 == 0


def engine_compatible_meta(
    is_mp4: bool, size: int, info: VideoInfo, policy: VideoPolicy
) -> bool:
    """Whether the engine can keep a source of this size/shape without re-encoding."""
    if not is_mp4:
        return False
    if info.video_codec != "h264" or info.pix_fmt != "yuv420p":
        return False
    if not _even(info.width) or not _even(info.height):
        return False
    if info.rotation:
        return False
    if info.sample_aspect not in {"", "1:1", "0:1"}:
        return False
    if info.profile and info.profile not in _ENGINE_PROFILES:
        return False
    if policy.max_edge > 0 and max(info.width, info.height) > policy.max_edge:
        return False
    if policy.max_fps > 0 and info.fps > policy.max_fps + 0.01:
        return False
    if info.duration > policy.max_seconds:
        return False
    if policy.max_passthrough_bytes > 0 and size > policy.max_passthrough_bytes:
        return False
    if info.audio_codec is not None and (
        info.audio_codec != "aac"
        or info.audio_rate not in _ENGINE_SAMPLE_RATES
        or (info.audio_channels or 0) > 2
    ):
        return False
    return True


def engine_compatible(data: bytes, info: VideoInfo, policy: VideoPolicy) -> bool:
    """Whether the engine can keep these bytes without re-encoding."""
    return engine_compatible_meta(_is_mp4(data), len(data), info, policy)


def _video_filter(policy: VideoPolicy) -> str:
    if policy.max_edge > 0:
        edge = policy.max_edge
        filters = [
            f"scale=w='min({edge},iw)':h='min({edge},ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2"
        ]
    else:
        filters = ["pad=ceil(iw/2)*2:ceil(ih/2)*2"]
    filters.append("setsar=1")
    if policy.max_fps > 0:
        filters.append(f"fps={policy.max_fps:g}")
    return ",".join(filters)


def _video_bitrate_cap(policy: VideoPolicy, duration: float) -> int | None:
    """Keep long clips under an explicit output budget; no budget means no cap."""
    if policy.max_output_bytes <= 0 or duration <= 0:
        return None
    budget = policy.max_output_bytes * 8 * 0.85
    bitrate = int(budget / duration) - policy.audio_kbps * 1000
    return max(250_000, min(2_500_000, bitrate))


def _encode_command(
    policy: VideoPolicy, source: Path, output: Path, info: VideoInfo
) -> list[str]:
    assert policy.ffmpeg is not None
    cap = _video_bitrate_cap(policy, info.duration)
    rate_args = ["-maxrate", str(cap), "-bufsize", str(cap * 2)] if cap else []
    audio_args: list[str] = []
    if info.audio_codec is not None:
        channels = min(2, max(1, info.audio_channels or 2))
        audio_args = [
            "-c:a",
            "aac",
            "-b:a",
            f"{policy.audio_kbps}k",
            "-ac",
            str(channels),
            "-ar",
            "48000" if info.audio_rate not in {44100, 48000} else str(info.audio_rate),
        ]
    return [
        policy.ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-sn",
        "-dn",
        "-vf",
        _video_filter(policy),
        "-c:v",
        "libx264",
        "-preset",
        policy.preset,
        "-crf",
        str(policy.crf),
        *rate_args,
        "-profile:v",
        "main",
        "-pix_fmt",
        "yuv420p",
        "-threads",
        str(max(1, min(8, os.cpu_count() or 4))),
        *audio_args,
        "-movflags",
        "+faststart",
        "-max_muxing_queue_size",
        "1024",
        "-progress",
        "pipe:1",
        "-nostats",
        "-f",
        "mp4",
        str(output),
    ]


async def _watch_progress(
    stream: asyncio.StreamReader | None, duration: float, total: int | None
) -> None:
    if stream is None:
        return
    milestones = {0.25, 0.5, 0.75}
    seen: set[float] = set()
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode("ascii", "replace").strip()
        if text.startswith("out_time_us="):
            raw = text.partition("=")[2]
        elif text.startswith("out_time_ms="):
            raw = text.partition("=")[2]
        else:
            continue
        try:
            # Both keys are microseconds in ffmpeg's progress protocol.
            seconds = int(raw) / 1_000_000
        except ValueError:
            continue
        if duration <= 0:
            continue
        ratio = seconds / duration
        for milestone in milestones - seen:
            if ratio >= milestone:
                seen.add(milestone)
                if total and total > 0:
                    logger.info(
                        "微信消息适配器 视频转码进度：%.0f%% (%s/%s)",
                        milestone * 100,
                        _human_bytes(int(total * milestone)),
                        _human_bytes(total),
                    )
                else:
                    logger.info("微信消息适配器 视频转码进度：%.0f%%", milestone * 100)


def _effective_timeout(policy: VideoPolicy, duration: float) -> float:
    """Give longer clips a proportional budget instead of always 120 s."""
    return min(900.0, max(policy.timeout, duration * 3))


async def _run_encode(
    command: list[str], timeout: float, info: VideoInfo, output_size: int | None
) -> None:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, 8192))
    progress_task = asyncio.create_task(
        _watch_progress(process.stdout, info.duration, output_size)
    )
    try:
        async with asyncio.timeout(timeout):
            await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise VideoEncodeTimeout(f"转码超过 {timeout:g} 秒") from None
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await asyncio.gather(stderr_task, progress_task, return_exceptions=True)
    stderr = await stderr_task
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip()[:400]
        raise VideoEncodeError(f"ffmpeg 失败：{detail or process.returncode}")


def _human_bytes(value: int) -> str:
    if value >= MiB:
        return f"{value / MiB:.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.0f} KiB"
    return f"{value} B"


def _fastpath_ready(policy: VideoPolicy) -> bool:
    return (
        policy.enabled
        and policy.transcode
        and bool(policy.ffmpeg)
        and bool(policy.ffprobe)
    )


async def normalize_video(
    data: bytes, content_type: str, policy: VideoPolicy
) -> tuple[bytes, str]:
    """Return bytes the engine can send, normalizing only when it pays off.

    With the default ``transcode=False`` this is a no-op: Python never adds a
    lossy generation. Any failure also returns the original bytes.
    """
    if not policy.enabled or content_type not in _VIDEO_CONTENT_TYPES:
        return data, content_type
    if not policy.transcode:
        return data, content_type
    if not _fastpath_ready(policy):
        return data, content_type
    if len(data) > policy.max_input_bytes:
        logger.warning(
            "微信消息适配器 视频超过快速通道输入上限，交给 Native 处理：bytes=%s limit=%s",
            len(data),
            _human_bytes(policy.max_input_bytes),
        )
        return data, content_type

    async with _ENCODER_SLOT:
        try:
            with tempfile.TemporaryDirectory(prefix="webridge-video-") as directory:
                source = Path(directory) / "source"
                await asyncio.to_thread(source.write_bytes, data)
                info = await _probe_file(source, policy)
                if engine_compatible_meta(_is_mp4(data), len(data), info, policy):
                    logger.info(
                        "微信消息适配器 视频快速通道：源文件已兼容，直接复用 "
                        "codec=%s %sx%s %sfps %s",
                        info.video_codec,
                        info.width,
                        info.height,
                        f"{info.fps:.3g}",
                        _human_bytes(len(data)),
                    )
                    return data, content_type
                return await _encode_normalized(source, len(data), info, policy)
        except asyncio.CancelledError:
            raise
        except VideoEncodeTimeout as error:
            logger.warning(
                "微信消息适配器 视频快速通道超时，改由 Native 处理（可能较慢）：%s", error
            )
            return data, content_type
        except (VideoEncodeError, OSError, ValueError) as error:
            logger.warning("微信消息适配器 视频快速通道不可用，改由 Native 处理：%s", error)
            return data, content_type


async def normalize_video_path(
    path: Path, content_type: str, policy: VideoPolicy
) -> tuple[bytes, str] | None:
    """Normalize a local file without reading the source into Python memory.

    Returns ``None`` when the fast path does not apply (disabled, no ffmpeg or
    larger than the input budget); the caller then keeps its generic path.
    Once ffprobe has accepted the file, encode failures return the original
    bytes instead, so the engine still gets a chance to send it.
    """
    if not policy.enabled or not policy.transcode:
        return None
    if not _fastpath_ready(policy) or content_type not in _VIDEO_CONTENT_TYPES:
        return None
    try:
        size = await asyncio.to_thread(lambda: path.stat().st_size)
    except OSError:
        return None
    if size <= 0 or size > policy.max_input_bytes:
        return None

    async with _ENCODER_SLOT:
        try:
            info = await _probe_file(path, policy)
            is_mp4 = await asyncio.to_thread(_is_mp4_file, path)
            if engine_compatible_meta(is_mp4, size, info, policy):
                logger.info(
                    "微信消息适配器 视频快速通道：源文件已兼容，直接复用 "
                    "codec=%s %sx%s %sfps %s",
                    info.video_codec,
                    info.width,
                    info.height,
                    f"{info.fps:.3g}",
                    _human_bytes(size),
                )
                return await asyncio.to_thread(path.read_bytes), "video/mp4"
            return await _encode_normalized(path, size, info, policy)
        except asyncio.CancelledError:
            raise
        except VideoEncodeTimeout as error:
            logger.warning(
                "微信消息适配器 视频快速通道超时，改用原始视频交给 Native（可能较慢）：%s",
                error,
            )
        except (VideoEncodeError, OSError, ValueError) as error:
            logger.warning(
                "微信消息适配器 视频快速通道不可用，改用原始视频交给 Native：%s", error
            )
    try:
        return await asyncio.to_thread(path.read_bytes), content_type
    except OSError:
        return None


async def _encode_normalized(
    source: Path, original_size: int, info: VideoInfo, policy: VideoPolicy
) -> tuple[bytes, str]:
    started = asyncio.get_running_loop().time()
    output = source.with_name("normal.mp4")
    command = _encode_command(policy, source, output, info)
    timeout = _effective_timeout(policy, info.duration)
    logger.info(
        "微信消息适配器 视频快速通道：开始转码 codec=%s %sx%s %sfps -> "
        "edge=%s fps=%s crf=%s preset=%s bytes=%s timeout=%.0fs",
        info.video_codec,
        info.width,
        info.height,
        f"{info.fps:.3g}",
        "source" if policy.max_edge <= 0 else policy.max_edge,
        "source" if policy.max_fps <= 0 else f"{policy.max_fps:g}",
        policy.crf,
        policy.preset,
        _human_bytes(original_size),
        timeout,
    )
    await _run_encode(
        command, timeout, info, policy.max_output_bytes or None
    )
    output_size = await asyncio.to_thread(lambda: output.stat().st_size)
    if output_size <= 0:
        raise VideoEncodeError("转码输出为空")
    if 0 < policy.max_output_bytes < output_size:
        raise VideoEncodeError(
            f"转码输出 {_human_bytes(output_size)} 超过上限 "
            f"{_human_bytes(policy.max_output_bytes)}"
        )
    encoded = await asyncio.to_thread(output.read_bytes)
    if len(encoded) != output_size:
        raise VideoEncodeError("转码输出在读取时发生变化")
    elapsed = asyncio.get_running_loop().time() - started
    logger.info(
        "微信消息适配器 视频快速通道：完成 %.1fs，%s -> %s (%.0f%%)",
        elapsed,
        _human_bytes(original_size),
        _human_bytes(len(encoded)),
        len(encoded) * 100 / original_size if original_size else 0,
    )
    return encoded, "video/mp4"
