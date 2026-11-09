"""agent-wechat-native v1.5.1 /v1 transport; no AstrBot OpenAPI dependency."""

import asyncio
import base64
import binascii
import hashlib
import io
import ipaddress
import json
import re
import socket
import wave
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit
from urllib.request import url2pathname
from uuid import UUID

import aiohttp
from aiohttp.http_exceptions import LineTooLong
from astrbot.api import logger

from .config import (
    MAX_IMAGE_BYTES,
    MAX_VOICE_BYTES,
    MAX_VOICE_SECONDS,
    MEDIA_DOWNLOAD_TIMEOUT,
    VIDEO_DOWNLOAD_TIMEOUT,
    VIDEO_PROTOCOL_MAX_BYTES,
    VIDEO_SEND_TIMEOUT,
    VOICE_SEND_TIMEOUT,
)
from .state import cursor_number

_SAFE_CODES = {
    "operation_in_progress",
    "idempotency_conflict",
    "login_challenge_unavailable",
    "login_challenge_superseded",
    "account_sign_out_required",
    "auth_required",
    "event_cursor_expired",
    "invalid_cursor",
    "unauthorized",
    "forbidden",
    "not_found",
    "timeout",
    "account_not_found",
    "insufficient_scope",
    "media_not_cached",
    "media_not_available",
    "media_asset_not_found",
    "media_preview_unavailable",
    "media_variant_unavailable",
    "media_metadata_unavailable",
    "media_metadata_invalid",
    "media_metadata_limit",
    "media_key_missing",
    "media_identity_unavailable",
    "media_source_changed",
    "media_encoder_unavailable",
    "media_expired",
    "media_integrity_failed",
    "message_recalled",
    "recall_identity_mismatch",
    "recall_target_changed",
    "recall_target_unavailable",
    "unsupported_media",
    "unsupported_application_media",
    "unsupported_attachment_encryption",
    "unsupported_attachment_transport",
    "image_quality_unavailable",
    "image_size_limit",
    "source_size_limit",
    "image_decode_failed",
    "image_decode_limit",
    "image_format_unsupported",
    "wxgf_invalid",
    "wxgf_decode_failed",
    "wxgf_decoder_unavailable",
    "wxgf_decode_timeout",
    "wxgf_preview_limit",
    "mention_member_unavailable",
    "mention_group_membership_unavailable",
    "group_contact_unavailable",
    "group_state_incomplete",
    "voice_duration_limit",
    "media_audio_invalid",
    "unsupported_voice_chunk",
}

MEDIA_ERROR_MESSAGES = {
    "unavailable": "图片无法读取，请重新发送。",
    "expired": "引用的图片已失效，请重新发送。",
    "too_large": "图片太大，暂时无法处理。",
    "missing_scope": "图片读取功能未正确配置，请联系管理员。",
    "unsupported_format": "暂不支持这种图片格式。",
    "recalled": "",
    "recent_unavailable": "未找到你在本群最近2分钟发送的图片，请重新发送或引用图片。",
}

VIDEO_ERROR_MESSAGES = {
    "unavailable": "视频无法读取，请重新发送。",
    "expired": "引用的视频已失效，请重新发送。",
    "too_large": "视频超出当前传输协议可表示的大小。",
    "missing_scope": "视频读取功能未正确配置，请联系管理员。",
    "unsupported_format": "暂不支持这种视频格式。",
    "recalled": "",
}

VOICE_ERROR_MESSAGES = {
    "unavailable": "语音无法读取，请重新发送。",
    "expired": "引用的语音已失效，请重新发送。",
    "too_large": "语音太大或时长超限，暂时无法处理。",
    "missing_scope": "语音读取功能未正确配置，请联系管理员。",
    "unsupported_format": "暂不支持这种音频格式。",
    "recalled": "",
    "recent_unavailable": "未找到你在本群最近2分钟发送的语音，请重新发送或引用语音。",
}

_VARIANT_RANK = {"original": 0, "standard": 1, "thumbnail": 2}
_NON_TRANSFERABLE_STATES = {
    "available",
    "unavailable",
    "expired",
    "unsupported",
    "recalled",
    "failed",
}


class NativeAPIError(Exception):
    def __init__(self, status: int, code: str = "request_failed"):
        self.status = status
        self.code = code if code in _SAFE_CODES else "request_failed"
        super().__init__(f"Native API HTTP {status}: {self.code}")


class MediaError(Exception):
    def __init__(self, reason: str = "unavailable", *, media_kind: str = "image"):
        self.media_kind = media_kind if media_kind in {"video", "voice"} else "image"
        messages = {"video": VIDEO_ERROR_MESSAGES, "voice": VOICE_ERROR_MESSAGES}.get(
            self.media_kind, MEDIA_ERROR_MESSAGES
        )
        self.reason = reason if reason in messages else "unavailable"
        self.user_message = messages[self.reason]
        super().__init__(f"Native media error: {self.reason}")


class QuotedMediaMetadataError(MediaError):
    """Native rejected the embedded quote's download metadata."""

    def __init__(self, *, media_kind: str = "image"):
        super().__init__("unavailable", media_kind=media_kind)


class SendError(Exception):
    """A retained send requires inspection. Never automatically create a new key."""

    def __init__(self, state: str, key: str, send_id: str | None = None, *, chunks=None):
        self.state, self.key, self.send_id = state, key, send_id
        self.chunks = chunks
        super().__init__(f"Native send requires inspection: {state}")


class GroupMembersUnavailable(ValueError):
    """Mention metadata could not be prepared; no send has been submitted."""

    def __init__(self, state: str, code: str = "operation_failed", operation_id: str | None = None):
        self.state = state
        self.code = "operation_failed"
        if isinstance(code, str) and code in {
            "operation_failed",
            "auth_required",
            "account_identity_mismatch",
            "members_unavailable",
            "timeout",
        }:
            self.code = code
        self.operation_id = None
        if operation_id is not None:
            try:
                self.operation_id = str(UUID(operation_id))
            except ValueError:
                pass
        super().__init__(
            f"Native 群成员资料不可用：状态 {self.state}，错误码 {self.code}，"
            f"操作 {self.operation_id or 'unknown'}"
        )


def _check_guard(guard) -> None:
    if guard is not None:
        guard()


def _effective_variant(asset: dict) -> str:
    actual = asset.get("actualVariant")
    if isinstance(actual, str) and actual:
        return actual
    variant = asset.get("variant")
    return variant if isinstance(variant, str) else ""


def _extract_job_error_code(job: dict) -> str:
    for key in ("error", "result"):
        val = job.get(key)
        if isinstance(val, dict) and isinstance(val.get("code"), str):
            return val["code"]
        if isinstance(val, str) and val:
            return val
    for key in ("reason", "errorCode", "code"):
        val = job.get(key)
        if isinstance(val, str) and val:
            return val
    return "request_failed"


def _map_media_code(
    code: str | None, *, quoted: bool = False, status: int = 0, media_kind: str = "image"
) -> MediaError:
    if status in {401, 403} or code in {"unauthorized", "forbidden", "insufficient_scope"}:
        return MediaError("missing_scope", media_kind=media_kind)
    if code == "message_recalled" or (status == 410 and code != "event_cursor_expired"):
        return MediaError("recalled", media_kind=media_kind)
    if status == 413 or code in {
        "image_size_limit",
        "source_size_limit",
        "wxgf_preview_limit",
        "voice_duration_limit",
    }:
        return MediaError("too_large", media_kind=media_kind)
    if code == "media_expired" or (
        quoted
        and status == 404
        and code in {"not_found", "media_not_cached", "media_asset_not_found"}
    ):
        return MediaError("expired", media_kind=media_kind)
    if code in {
        "unsupported_media",
        "unsupported_application_media",
        "unsupported_attachment_encryption",
        "unsupported_attachment_transport",
        "image_format_unsupported",
        "wxgf_invalid",
        "wxgf_decode_failed",
        "wxgf_decoder_unavailable",
        "wxgf_decode_timeout",
        "media_audio_invalid",
        "unsupported_voice_chunk",
    }:
        return MediaError("unsupported_format", media_kind=media_kind)
    return MediaError("unavailable", media_kind=media_kind)


def validate_video_bytes(data: bytes, *, max_bytes: int = VIDEO_PROTOCOL_MAX_BYTES) -> str:
    """Check the MOV-family container; Native owns codec checks and preparation."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise MediaError("unavailable", media_kind="video")
    if len(data) > max_bytes:
        raise MediaError("too_large", media_kind="video")
    offset = boxes = 0
    movie = media = file_type = False
    content_type = "video/quicktime"
    while offset < len(data):
        boxes += 1
        if boxes > 65536 or len(data) - offset < 8:
            raise MediaError("unsupported_format", media_kind="video")
        size = int.from_bytes(data[offset : offset + 4], "big")
        header = 8
        if size == 0:
            size = len(data) - offset
        elif size == 1:
            if len(data) - offset < 16:
                raise MediaError("unsupported_format", media_kind="video")
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header = 16
        if size < header or size > len(data) - offset:
            raise MediaError("unsupported_format", media_kind="video")
        kind = data[offset + 4 : offset + 8]
        if kind == b"ftyp":
            if file_type or size < header + 8 or (size - header) % 4:
                raise MediaError("unsupported_format", media_kind="video")
            file_type = True
            brand = data[offset + header : offset + header + 4]
            if brand == b"qt  ":
                content_type = "video/quicktime"
            elif brand.startswith(b"3g2"):
                content_type = "video/3gpp2"
            elif brand.startswith(b"3g"):
                content_type = "video/3gpp"
            elif brand in {b"mjp2", b"mj2s"}:
                content_type = "video/mj2"
            else:
                content_type = "video/mp4"
        elif kind == b"moov":
            movie = size > header
        elif kind == b"mdat":
            media |= size > header
        offset += size
    if not movie or not media:
        raise MediaError("unsupported_format", media_kind="video")
    return content_type


def _jpeg_has_end_marker(data: bytes | bytearray) -> bool:
    """Walk JPEG framing; metadata may contain EOI bytes and a photo may have a trailer."""
    position = 2
    in_scan = False
    saw_scan = False
    while position < len(data):
        if in_scan:
            position = data.find(b"\xff", position)
            if position < 0:
                return False
        if data[position] != 0xFF:
            return False
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position == len(data):
            return False
        marker = data[position]
        position += 1
        if in_scan and (marker == 0x00 or 0xD0 <= marker <= 0xD7):
            continue
        if marker == 0xD9:
            return saw_scan
        if marker == 0x00 or 0xD0 <= marker <= 0xD8:
            return False
        if marker == 0x01:
            continue
        if position + 2 > len(data):
            return False
        length = int.from_bytes(data[position : position + 2], "big")
        if length < 2 or position + length > len(data):
            return False
        position += length
        if marker == 0xDA:
            saw_scan = True
            in_scan = True
        elif marker != 0xDC:
            in_scan = False
    return False


def validate_image_bytes(data: bytes, *, max_bytes: int = MAX_IMAGE_BYTES) -> str:
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise MediaError("unavailable")
    if len(data) > max_bytes:
        raise MediaError("too_large")
    if data[:4].lower() == b"wxgf" or data.lstrip()[:1] == b"<":
        raise MediaError("unsupported_format")
    if data.startswith(b"\xff\xd8\xff"):
        if len(data) < 12 or not _jpeg_has_end_marker(data):
            raise MediaError("unavailable")
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if (
            len(data) < 33
            or data[12:16] != b"IHDR"
            or int.from_bytes(data[16:20], "big") <= 0
            or int.from_bytes(data[20:24], "big") <= 0
            or not data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
        ):
            raise MediaError("unavailable")
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        if (
            len(data) < 14
            or int.from_bytes(data[6:8], "little") <= 0
            or int.from_bytes(data[8:10], "little") <= 0
            or not data.endswith(b";")
        ):
            raise MediaError("unavailable")
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        if (
            len(data) < 16
            or data[12:16] not in {b"VP8 ", b"VP8L", b"VP8X"}
            or int.from_bytes(data[4:8], "little") + 8 != len(data)
        ):
            raise MediaError("unavailable")
        return "image/webp"
    if data.startswith((b"\x89PNG", b"GIF8")):
        raise MediaError("unavailable")
    raise MediaError("unsupported_format")


def _normalize_streaming_wav(data: bytes) -> bytes:
    """Rewrite placeholder RIFF/data chunk sizes so the daemon can parse streaming WAV."""
    if not (data.startswith(b"RIFF") and data[8:12] == b"WAVE"):
        return data
    total = len(data)
    if int.from_bytes(data[4:8], "little") + 8 <= total:
        return data
    out = bytearray(data)
    out[4:8] = (total - 8).to_bytes(4, "little")
    index = 12
    while index + 8 <= total:
        chunk_id = bytes(out[index : index + 4])
        chunk_size = int.from_bytes(out[index + 4 : index + 8], "little")
        available = total - index - 8
        if chunk_id == b"data":
            out[index + 4 : index + 8] = available.to_bytes(4, "little")
            break
        if chunk_size > available:
            break
        index += 8 + chunk_size + (chunk_size & 1)
    return bytes(out)


def validate_voice_bytes(data: bytes, *, normal: bool = False) -> str:
    """Check containers; Native owns decoding and duration checks for compressed audio."""
    if not data:
        raise MediaError("unavailable", media_kind="voice")
    if len(data) > MAX_VOICE_BYTES:
        raise MediaError("too_large", media_kind="voice")
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        if normal and int.from_bytes(data[4:8], "little") + 8 > len(data):
            raise MediaError("unavailable", media_kind="voice")
        try:
            with wave.open(io.BytesIO(data)) as audio:
                declared_frames, rate = audio.getnframes(), audio.getframerate()
                channels, width = audio.getnchannels(), audio.getsampwidth()
                if not rate or not channels or not width:
                    raise MediaError("unavailable", media_kind="voice")
                pcm = audio.readframes(len(data))
                if normal and len(pcm) != declared_frames * channels * width:
                    raise MediaError("unavailable", media_kind="voice")
                frames = len(pcm) // (channels * width)
                if not frames or len(pcm) % (channels * width):
                    raise MediaError("unavailable", media_kind="voice")
                if frames > rate * (60 if normal else MAX_VOICE_SECONDS):
                    raise MediaError("too_large", media_kind="voice")
                if normal and (channels, width, rate) != (1, 2, 16000):
                    raise MediaError("unsupported_format", media_kind="voice")
            return "audio/wav"
        except wave.Error as error:
            if not normal and str(error).startswith("unknown format:"):
                # Float/compressed WAV from TTS is valid input to Native's FFmpeg
                # path even when Python's PCM-only reader cannot open it.
                return "audio/wav"
            raise MediaError("unsupported_format", media_kind="voice") from None
        except EOFError:
            raise MediaError("unsupported_format", media_kind="voice") from None
    if not normal:
        if data.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
            return "audio/silk"
        if data.startswith(b"fLaC"):
            return "audio/flac"
        if data.startswith(b"OggS"):
            return "audio/ogg"
        if len(data) > 2 and data[0] == 0xFF and data[1] & 0xF6 == 0xF0:
            return "audio/aac"
        if data.startswith(b"ID3") or (
            len(data) > 2
            and data[0] == 0xFF
            and data[1] & 0xE0 == 0xE0
            and data[1] & 6 != 0
            and data[1] & 0x18 != 8
        ):
            return "audio/mpeg"
        if data[4:8] == b"ftyp":
            return "audio/mp4"
        if data.startswith(b"\x1aE\xdf\xa3"):
            return "audio/webm"
    raise MediaError("unsupported_format", media_kind="voice")


def _public_media_address(host: str) -> None:
    address = ipaddress.ip_address(host)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if not address.is_global or address.is_multicast or address.is_unspecified:
        raise ValueError("媒体 URL 只允许公网地址")


class _PublicMediaResolver(aiohttp.abc.AbstractResolver):
    """Validate the actual connector DNS answers, avoiding a second DNS lookup."""

    def __init__(self):
        self.resolver = aiohttp.resolver.DefaultResolver()
        self.trusted_renderer_host = None

    async def resolve(self, host, port=0, family=socket.AF_INET):
        results = await self.resolver.resolve(host, port, family)
        if not results:
            raise ValueError("媒体 URL 无可用公网地址")
        if host != self.trusted_renderer_host:
            for result in results:
                _public_media_address(result["host"])
        return results

    async def close(self):
        await self.resolver.close()


async def load_outgoing_image(image) -> tuple[bytes, str]:
    return await _load_outgoing_media(image.url or image.file or image.path, kind="image")


async def load_outgoing_video(video) -> tuple[bytes, str]:
    # _resolve_file_source does no downloading. Keep our credential-free
    # URL reader instead of Video.convert_to_file_path's general downloader.
    return await _load_outgoing_media(await video._resolve_file_source(), kind="video")


async def load_outgoing_voice(record) -> tuple[bytes, str]:
    # Match Record's file -> url -> path fallback without using its URL downloader
    # or converting TTS output locally. Native owns decoding, duration and SILK.
    source = None
    for candidate in (record.file, record.url, record.path):
        if not isinstance(candidate, str) or not candidate:
            continue
        if candidate.startswith(("file:", "http:", "https:", "base64://", "data:")):
            source = candidate
            break
        try:
            if len(candidate) < 4096 and await asyncio.to_thread(Path(candidate).is_file):
                source = candidate
                break
        except OSError:
            pass
    return await _load_outgoing_media(
        source or record.file or record.url or record.path, kind="voice"
    )


async def _load_outgoing_media(source, *, kind: str) -> tuple[bytes, str]:
    """Resolve standard media references with bounded local and public URL reads."""
    max_bytes = {
        "voice": MAX_VOICE_BYTES,
        "image": MAX_IMAGE_BYTES,
        "video": VIDEO_PROTOCOL_MAX_BYTES,
    }[kind]
    label = {"voice": "语音", "image": "图片", "video": "视频"}[kind]
    if not isinstance(source, str) or not source:
        raise ValueError(f"{label}来源为空")
    try:
        if source.lower().startswith(("http:", "https:")):
            data = (
                await _download_public_media(
                    source, max_bytes=max_bytes, read_budget=VIDEO_DOWNLOAD_TIMEOUT
                )
                if kind == "video"
                else await _download_public_media(source, max_bytes=max_bytes)
            )
        elif source.startswith(("base64://", "data:")):
            if source.startswith("data:"):
                header, separator, encoded = source.partition(",")
                if (
                    not separator
                    or not header.lower().startswith(
                        {"voice": "data:audio/", "video": "data:video/", "image": "data:image/"}[
                            kind
                        ]
                    )
                    or not header.lower().endswith(";base64")
                ):
                    raise ValueError("媒体 data URI 必须使用匹配的 MIME 和 base64")
            else:
                encoded = source[len("base64://") :]
            data = _decode_media_base64(encoded, max_bytes=max_bytes)
        else:
            if source.lower().startswith("file:"):
                uri = urlsplit(source)
                if uri.netloc or uri.query or uri.fragment:
                    raise ValueError("不支持网络媒体路径")
                source = url2pathname(uri.path)
            elif "://" in source:
                raise ValueError("媒体 URL 只支持 HTTP(S)")
            if source.startswith(("\\\\", "//")):
                raise ValueError("不支持网络媒体路径")
            path = Path(source)
            # AstrBot also accepts legacy bare base64; prefer an existing local path.
            try:
                local_file = len(source) < 4096 and await asyncio.to_thread(path.is_file)
            except OSError:
                local_file = False  # Long bare base64 can exceed an OS filename limit.
            if local_file:
                data = await asyncio.to_thread(_read_media_file, path, max_bytes)
            else:
                data = _decode_media_base64(source, max_bytes=max_bytes)
        content_type = {
            "voice": validate_voice_bytes,
            "video": validate_video_bytes,
            "image": validate_image_bytes,
        }[kind](data)
        return bytes(data), content_type
    except MediaError as error:
        raise ValueError(f"发送{label}校验失败：{error.reason}") from None
    except (OSError, aiohttp.ClientError, TimeoutError, binascii.Error):
        raise ValueError(f"发送{label}读取失败或超时，未提交发送") from None


def _decode_media_base64(encoded: str, *, max_bytes: int) -> bytes:
    if len(encoded) > ((max_bytes + 2) // 3) * 4 + 1024:
        raise ValueError(f"发送媒体超过大小限制 ({max_bytes} bytes)")
    compact = "".join(encoded.split())
    return base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True)


def _read_media_file(path: Path, max_bytes: int) -> bytes:
    if path.stat().st_size > max_bytes:
        raise ValueError(f"发送媒体超过大小限制 ({max_bytes} bytes)")
    with path.open("rb") as stream:
        return stream.read(max_bytes + 1)


async def _download_public_media(url: str, *, max_bytes: int, read_budget: float = 15) -> bytes:
    resolver = _PublicMediaResolver()
    try:
        async with (
            aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    resolver=resolver, use_dns_cache=False, force_close=True
                ),
                timeout=aiohttp.ClientTimeout(
                    total=read_budget, connect=5, sock_read=min(read_budget, 30)
                ),
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                auto_decompress=False,
            ) as session,
            asyncio.timeout(read_budget),
        ):
            for redirects in range(4):
                parsed = urlsplit(url)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or (parsed.port is not None and not 1 <= parsed.port <= 65535)
                    or "\\" in url
                    or any(ord(char) < 33 or ord(char) == 127 for char in url)
                ):
                    raise ValueError("媒体 URL 协议、端口或格式不允许")
                # AstrBot's renderer pool can use custom ports, Docker DNS or a
                # local service. Only its configured endpoint path is trusted;
                # re-evaluate every redirect without carrying credentials.
                trusted = _is_renderer_media_url(url)
                resolver.trusted_renderer_host = parsed.hostname if trusted else None
                try:
                    ipaddress.ip_address(parsed.hostname)
                except ValueError:
                    pass  # Hostnames are checked by the connector's resolver.
                else:
                    if not trusted:
                        _public_media_address(parsed.hostname)
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if redirects == 3 or not location:
                            raise ValueError("媒体 URL 重定向过多或无效")
                        url = urljoin(url, location)
                        continue
                    if response.status != 200:
                        raise ValueError("媒体 URL 下载失败")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise ValueError("媒体 URL 不支持压缩传输")
                    if response.content_length is not None and response.content_length > max_bytes:
                        raise ValueError(f"发送媒体超过大小限制 ({max_bytes} bytes)")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        if len(data) + len(chunk) > max_bytes:
                            raise ValueError(f"发送媒体超过大小限制 ({max_bytes} bytes)")
                        data.extend(chunk)
                    return bytes(data)
    finally:
        await resolver.close()


def _is_renderer_media_url(url: str) -> bool:
    from astrbot.core import html_renderer

    strategy = html_renderer.network_strategy
    target = urlsplit(url)
    for endpoint in [strategy.BASE_RENDER_URL, *strategy.endpoints]:
        if not isinstance(endpoint, str):
            continue
        base = urlsplit(endpoint)
        if (
            target.scheme == base.scheme
            and target.hostname == base.hostname
            and target.port == base.port
            and target.path.startswith(base.path.rstrip("/") + "/")
            and all(segment not in {".", ".."} for segment in unquote(target.path).split("/"))
        ):
            return True
    return False


class NativeClient:
    def __init__(
        self,
        api_url: str,
        account_id: str,
        token: str,
        *,
        request_timeout: float = 15,
        send_timeout: float = 65,
        media_timeout: float = MEDIA_DOWNLOAD_TIMEOUT,
        poll_interval: float = 0.25,
        idle_timeout: float = 45,
        voice_send_timeout: float = VOICE_SEND_TIMEOUT,
        video_send_timeout: float = VIDEO_SEND_TIMEOUT,
        video_timeout: float = VIDEO_DOWNLOAD_TIMEOUT,
    ):
        self.api_url = api_url
        self.account_id = account_id
        self.prefix = f"/accounts/{quote(account_id, safe='')}"
        self._token = token
        self.request_timeout = request_timeout
        self.send_timeout, self.poll_interval = send_timeout, poll_interval
        self.media_timeout = media_timeout
        self.voice_send_timeout = voice_send_timeout
        self.video_send_timeout = video_send_timeout
        self.video_timeout = video_timeout
        self.idle_timeout = idle_timeout
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def open(self):
        if self.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=self.request_timeout),
                trust_env=False,  # Native commonly lives on a private Docker network.
                cookie_jar=aiohttp.DummyCookieJar(),
            )

    @property
    def closed(self):
        return self._session is None or self._session.closed

    async def close(self):
        if self._session:
            await self._session.close()

    async def _read_json(self, response) -> dict:
        data = bytearray()
        async for part in response.content.iter_chunked(16384):
            data.extend(part)
            if len(data) > 1024 * 1024:
                raise NativeAPIError(502)
        try:
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeDecodeError):
            raise NativeAPIError(502) from None

    async def _raise_for_status(self, response) -> None:
        if 200 <= response.status < 300:
            return
        # Never include a server body, URL or underlying exception in logs.
        default_code = (
            "event_cursor_expired"
            if response.status == 410
            else "image_size_limit"
            if response.status == 413
            else "request_failed"
        )
        code = default_code
        try:
            body = await self._read_json(response)
            error = body.get("error")
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                code = error["code"]
        except (NativeAPIError, aiohttp.ClientError, TimeoutError, OSError):
            pass  # Preserve the HTTP status even when its error body is unusable.
        if code not in _SAFE_CODES:
            code = default_code
        raise NativeAPIError(response.status, code)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        if self.closed:
            raise NativeAPIError(0)
        try:
            async with self._session.request(
                method, self.api_url + path, allow_redirects=False, **kwargs
            ) as response:
                await self._raise_for_status(response)
                return await self._read_json(response)
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise NativeAPIError(0, "timeout") from None

    async def accounts(self):
        return await self._request("GET", "/accounts")

    async def status(self):
        return await self._request("GET", "/status")

    async def account(self):
        return await self._request("GET", self.prefix)

    async def capabilities(self):
        return await self._request("GET", self.prefix + "/capabilities")

    async def message(self, message_id: str):
        return await self._request("GET", self.prefix + "/messages/" + quote(message_id, safe=""))

    async def operation(self, operation_id: str):
        return await self._request(
            "GET", self.prefix + "/operations/" + quote(operation_id, safe="")
        )

    async def login(self, key: str):
        return await self._request(
            "POST",
            self.prefix + "/auth/login",
            headers={"Idempotency-Key": key},
            json={
                "strategy": "qr",
                "forceQr": True,
                "allowQr": True,
                "timeoutSeconds": 300,
                "computerName": "WeChat Adapter",
                "qrRefresh": {"mode": "auto", "maxRefreshes": 3},
            },
        )

    async def cancel_operation(self, operation_id: str):
        return await self._request(
            "DELETE", self.prefix + "/operations/" + quote(operation_id, safe="")
        )

    async def logout(self, key: str):
        return await self._request(
            "POST", self.prefix + "/logout", headers={"Idempotency-Key": key}
        )

    async def login_qr(self, operation_id: str, generation: int) -> bytes:
        path = self.prefix + "/operations/" + quote(operation_id, safe="") + "/qr"
        try:
            async with self._session.get(
                self.api_url + path,
                params={"generation": str(generation)},
                allow_redirects=False,
            ) as response:
                await self._raise_for_status(response)
                data = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    data.extend(chunk)
                    if len(data) > 1024 * 1024:
                        raise NativeAPIError(502)
                if response.content_type != "image/png" or not data.startswith(
                    b"\x89PNG\r\n\x1a\n"
                ):
                    raise NativeAPIError(502)
                return bytes(data)
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise NativeAPIError(0, "timeout") from None

    async def media_assets(self, message_id: str) -> dict:
        return await self._request(
            "GET", self.prefix + "/messages/" + quote(message_id, safe="") + "/media"
        )

    async def transfer_media(
        self, message_id: str, quality: str = "original", *, asset_id: str | None = None
    ) -> dict:
        logger.debug(
            "微信消息适配器 请求媒体传输：message=%s quality=%s asset=%s",
            message_id,
            quality,
            asset_id,
        )
        payload = {"messageId": message_id, "quality": quality}
        if asset_id is not None:
            payload["assetId"] = asset_id
        try:
            return await self._request(
                "POST",
                self.prefix + "/messages/" + quote(message_id, safe="") + "/media-transfers",
                json=payload,
            )
        except NativeAPIError as error:
            logger.warning(
                "微信消息适配器 媒体传输请求失败：message=%s http_status=%s code=%s",
                message_id,
                error.status,
                error.code,
            )
            raise

    async def job(self, job_id: str) -> dict:
        return await self._request("GET", self.prefix + "/jobs/" + quote(job_id, safe=""))

    async def media_content(
        self,
        asset_id: str,
        *,
        preview: bool = False,
        max_bytes: int = MAX_IMAGE_BYTES,
        read_budget: float | None = None,
    ) -> tuple[bytes, str]:
        if self.closed:
            raise NativeAPIError(0)
        path = self.prefix + "/media/" + quote(asset_id, safe="") + "/content"
        params = {"representation": "preview"} if preview else None
        try:
            async with self._session.get(
                self.api_url + path,
                params=params,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=read_budget or self.media_timeout),
            ) as response:
                await self._raise_for_status(response)
                raw_length = response.headers.get("Content-Length")
                expected_length = None
                if raw_length is not None:
                    if not raw_length.strip().isdigit():
                        response.close()
                        raise NativeAPIError(502)
                    expected_length = int(raw_length)
                    if expected_length > max_bytes:
                        response.close()
                        raise MediaError("too_large")
                data = bytearray()
                chunk_size = max(1, min(65536, max_bytes + 1))
                async for chunk in response.content.iter_chunked(chunk_size):
                    if len(data) + len(chunk) > max_bytes:
                        response.close()
                        raise MediaError("too_large")
                    data.extend(chunk)
                if expected_length is not None and len(data) != expected_length:
                    raise MediaError("unavailable")
                return bytes(data), response.content_type or "application/octet-stream"
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise NativeAPIError(0, "timeout") from None

    async def _load_image_assets(self, message_id: str, *, quoted: bool, guard=None) -> list[dict]:
        return await self._load_assets(message_id, "image", quoted=quoted, guard=guard)

    async def _load_assets(
        self, message_id: str, kind: str, *, quoted: bool, guard=None
    ) -> list[dict]:
        _check_guard(guard)
        media_label = {"video": "视频", "voice": "语音", "image": "图片"}.get(kind, "媒体")
        media_kind = kind if kind in {"voice", "image", "video"} else "image"
        logger.debug(
            "微信消息适配器 查询%s资产：message=%s quoted=%s",
            media_label,
            message_id,
            quoted,
        )
        try:
            body = await self.media_assets(message_id)
        except NativeAPIError as error:
            if quoted and error.status == 409 and error.code == "media_metadata_invalid":
                raise QuotedMediaMetadataError() from None
            logger.warning(
                "微信消息适配器 %s资产查询失败：message=%s http_status=%s code=%s",
                media_label,
                message_id,
                error.status,
                error.code,
            )
            raise _map_media_code(
                error.code, quoted=quoted, status=error.status, media_kind=media_kind
            ) from None
        _check_guard(guard)
        items = body.get("items")
        if not isinstance(items, list):
            logger.warning("微信消息适配器 %s资产响应格式无效：message=%s", media_label, message_id)
            raise MediaError("unavailable", media_kind=media_kind)
        logger.debug(
            "微信消息适配器 %s资产查询完成：message=%s count=%s",
            media_label,
            message_id,
            len(items),
        )
        allowed_sources = {None, "quote"} if quoted else {None, "message"}
        matched = []
        for item in items:
            if (
                not isinstance(item, dict)
                or item.get("accountId") != self.account_id
                or item.get("messageId") != message_id
                or not isinstance(item.get("id"), str)
                or not item["id"]
            ):
                logger.warning(
                    "微信消息适配器 %s资产身份不匹配：message=%s", media_label, message_id
                )
                raise MediaError("unavailable", media_kind=media_kind)
            if item.get("kind") != kind or item.get("source") not in allowed_sources:
                continue
            reason = item.get("reason")
            logger.debug(
                "微信消息适配器 %s资产：message=%s asset=%s variant=%s actual_variant=%s "
                "state=%s content_type=%s bytes=%s code=%s",
                media_label,
                message_id,
                item["id"],
                item.get("variant"),
                item.get("actualVariant"),
                item.get("state"),
                item.get("contentType"),
                item.get("byteLength"),
                reason if isinstance(reason, str) and reason in _SAFE_CODES else None,
            )
            if item.get("state") == "recalled" or item.get("reason") == "message_recalled":
                raise MediaError("recalled", media_kind=media_kind)
            matched.append(item)
        if not matched:
            logger.warning(
                "微信消息适配器 未找到匹配的%s资产：message=%s quoted=%s",
                media_label,
                message_id,
                quoted,
            )
            raise MediaError("expired" if quoted else "unavailable", media_kind=media_kind)
        return matched

    async def _poll_media_job(
        self,
        job_id: str | None,
        *,
        message_id: str,
        initial: dict | None = None,
        asset_id: str | None = None,
        quoted: bool = False,
        guard=None,
    ) -> dict:
        if not isinstance(job_id, str) or not job_id:
            raise MediaError("unavailable")
        current = initial
        last_state = None
        while True:
            _check_guard(guard)
            if current is None:
                try:
                    current = await self.job(job_id)
                except NativeAPIError as error:
                    retryable = error.status in {0, 408, 429} or 500 <= error.status < 600
                    (logger.debug if retryable else logger.warning)(
                        "微信消息适配器 媒体任务查询失败：message=%s job=%s http_status=%s code=%s",
                        message_id,
                        job_id,
                        error.status,
                        error.code,
                    )
                    if retryable:
                        await asyncio.sleep(self.poll_interval)
                        continue
                    raise _map_media_code(error.code, quoted=quoted, status=error.status) from None
                _check_guard(guard)
            if (
                not isinstance(current, dict)
                or current.get("id") != job_id
                or current.get("accountId") != self.account_id
                or current.get("kind") not in {None, "download", "media"}
                or (current.get("messageId") is not None and current.get("messageId") != message_id)
                or (
                    asset_id is not None
                    and current.get("assetId") is not None
                    and current.get("assetId") != asset_id
                )
            ):
                logger.warning(
                    "微信消息适配器 媒体任务身份不匹配：message=%s job=%s",
                    message_id,
                    job_id,
                )
                raise MediaError("unavailable")
            state = current.get("state")
            if state != last_state:
                logger.debug(
                    "微信消息适配器 媒体任务状态：message=%s job=%s state=%s",
                    message_id,
                    job_id,
                    state,
                )
                last_state = state
            if state in {"available", "completed", "succeeded"}:
                return current
            if isinstance(state, str) and state in {
                "failed",
                "partial",
                "cancelled",
                "interrupted",
                "expired",
                "recalled",
                "needs_review",
            }:
                if state == "recalled":
                    raise MediaError("recalled")
                if state == "expired":
                    raise MediaError("expired")
                code = _extract_job_error_code(current)
                logger.warning(
                    "微信消息适配器 媒体任务失败：message=%s job=%s code=%s",
                    message_id,
                    job_id,
                    code if code in _SAFE_CODES else "request_failed",
                )
                raise _map_media_code(code, quoted=quoted)
            if not isinstance(state, str) or state not in {"queued", "running", "pending"}:
                raise MediaError("unavailable")
            current = None
            await asyncio.sleep(self.poll_interval)

    async def _download_validated_asset(
        self, asset: dict, *, max_bytes: int, quoted: bool, guard=None
    ) -> bytes:
        _check_guard(guard)
        is_wxgf = (
            asset.get("contentType") == "image/x-wxgf"
            or str(asset.get("format", "")).lower() == "wxgf"
        )
        logger.info(
            "微信消息适配器 下载图片资产：message=%s asset=%s variant=%s preview=%s",
            asset.get("messageId"),
            asset["id"],
            _effective_variant(asset),
            is_wxgf,
        )
        if is_wxgf:
            preview = asset.get("preview")
            if preview is True or asset.get("previewAvailable") is True:
                preview_meta = {}
            elif isinstance(preview, dict):
                preview_meta = preview
            else:
                raise MediaError("unsupported_format")
            if preview_meta.get("state") in {"unavailable", "unsupported", "failed"}:
                raise MediaError("unsupported_format")
            preview_type = preview_meta.get("contentType")
            if preview_type not in {None, "image/jpeg", "image/png", "image/webp", "image/gif"}:
                raise MediaError("unsupported_format")
            preview_len = preview_meta.get("byteLength")
            if isinstance(preview_len, int):
                if preview_len <= 0:
                    raise MediaError("unsupported_format")
                if preview_len > max_bytes:
                    raise MediaError("too_large")
            try:
                data, _ = await self.media_content(asset["id"], preview=True, max_bytes=max_bytes)
            except NativeAPIError as error:
                logger.warning(
                    "微信消息适配器 图片预览下载失败：asset=%s http_status=%s code=%s",
                    asset["id"],
                    error.status,
                    error.code,
                )
                raise _map_media_code(error.code, quoted=quoted, status=error.status) from None
            _check_guard(guard)
            if isinstance(preview_len, int) and len(data) != preview_len:
                logger.warning(
                    "微信消息适配器 图片预览长度不匹配：asset=%s expected=%s actual=%s",
                    asset["id"],
                    preview_len,
                    len(data),
                )
                raise MediaError("unavailable")
            preview_sha = preview_meta.get("sha256")
            if (
                isinstance(preview_sha, str)
                and preview_sha
                and hashlib.sha256(data).hexdigest().lower() != preview_sha.lower()
            ):
                logger.warning("微信消息适配器 图片预览散列校验失败：asset=%s", asset["id"])
                raise MediaError("unavailable")
        else:
            byte_len = asset.get("byteLength")
            if isinstance(byte_len, int):
                if byte_len <= 0:
                    raise MediaError("unavailable")
                if byte_len > max_bytes:
                    raise MediaError("too_large")
            adv_len = asset.get("advertisedByteLength")
            if isinstance(adv_len, int) and adv_len > max_bytes:
                raise MediaError("too_large")
            try:
                data, _ = await self.media_content(asset["id"], preview=False, max_bytes=max_bytes)
            except NativeAPIError as error:
                logger.warning(
                    "微信消息适配器 图片内容下载失败：asset=%s http_status=%s code=%s",
                    asset["id"],
                    error.status,
                    error.code,
                )
                raise _map_media_code(error.code, quoted=quoted, status=error.status) from None
            _check_guard(guard)
            if isinstance(byte_len, int) and len(data) != byte_len:
                logger.warning(
                    "微信消息适配器 图片内容长度不匹配：asset=%s expected=%s actual=%s",
                    asset["id"],
                    byte_len,
                    len(data),
                )
                raise MediaError("unavailable")
            sha = asset.get("sha256")
            if (
                isinstance(sha, str)
                and sha
                and hashlib.sha256(data).hexdigest().lower() != sha.lower()
            ):
                logger.warning("微信消息适配器 图片内容散列校验失败：asset=%s", asset["id"])
                raise MediaError("unavailable")
        try:
            content_type = validate_image_bytes(data, max_bytes=max_bytes)
        except MediaError as error:
            logger.warning(
                "微信消息适配器 图片格式校验失败：asset=%s bytes=%s reason=%s",
                asset["id"],
                len(data),
                error.reason,
            )
            raise
        logger.debug(
            "微信消息适配器 图片校验通过：asset=%s content_type=%s bytes=%s",
            asset["id"],
            content_type,
            len(data),
        )
        return data

    async def fetch_image(
        self,
        message_id: str,
        *,
        quoted: bool = False,
        max_bytes: int = MAX_IMAGE_BYTES,
        guard=None,
    ) -> bytes:
        try:
            async with asyncio.timeout(self.media_timeout):
                assets = await self._load_image_assets(message_id, quoted=quoted, guard=guard)
                errors: list[MediaError] = []
                tried_ids: set[str] = set()

                high_available = sorted(
                    (
                        a
                        for a in assets
                        if a.get("state") == "available"
                        and _effective_variant(a) in {"original", "standard"}
                    ),
                    key=lambda a: _VARIANT_RANK.get(_effective_variant(a), 99),
                )
                for asset in high_available:
                    tried_ids.add(asset["id"])
                    try:
                        return await self._download_validated_asset(
                            asset, max_bytes=max_bytes, quoted=quoted, guard=guard
                        )
                    except MediaError as error:
                        if error.reason in {"recalled", "missing_scope"}:
                            raise
                        errors.append(error)

                if not high_available:
                    transfer_candidates = sorted(
                        (
                            a
                            for a in assets
                            if a.get("variant") in {"original", "standard"}
                            and a.get("state") not in _NON_TRANSFERABLE_STATES
                        ),
                        key=lambda a: _VARIANT_RANK.get(a.get("variant", ""), 99),
                    )
                    if not transfer_candidates and not any(
                        a.get("variant") in {"original", "standard"} for a in assets
                    ):
                        transfer_candidates = [
                            a
                            for a in assets
                            if a.get("variant") == "thumbnail"
                            and a.get("state") not in _NON_TRANSFERABLE_STATES
                        ]
                        if not transfer_candidates and not quoted:
                            transfer_candidates = [{"id": None, "variant": "original"}]
                    for candidate in transfer_candidates:
                        size = (
                            candidate.get("byteLength")
                            if isinstance(candidate.get("byteLength"), int)
                            else candidate.get("advertisedByteLength")
                        )
                        if isinstance(size, int) and size > max_bytes:
                            errors.append(MediaError("too_large"))
                            continue
                        active_job = candidate.get("job")
                        active_job_id = (
                            candidate.get("jobId")
                            or candidate.get("activeJobId")
                            or (active_job.get("id") if isinstance(active_job, dict) else None)
                        )
                        try:
                            if isinstance(active_job_id, str) and active_job_id:
                                logger.debug(
                                    "微信消息适配器 复用媒体任务：message=%s job=%s",
                                    message_id,
                                    active_job_id,
                                )
                                await self._poll_media_job(
                                    active_job_id,
                                    message_id=message_id,
                                    initial=active_job if isinstance(active_job, dict) else None,
                                    asset_id=candidate.get("id"),
                                    quoted=quoted,
                                    guard=guard,
                                )
                            else:
                                _check_guard(guard)
                                try:
                                    job = await self.transfer_media(
                                        message_id, candidate.get("variant") or "original"
                                    )
                                except NativeAPIError as error:
                                    raise _map_media_code(
                                        error.code, quoted=quoted, status=error.status
                                    ) from None
                                _check_guard(guard)
                                await self._poll_media_job(
                                    job.get("id"),
                                    message_id=message_id,
                                    initial=job,
                                    asset_id=candidate.get("id"),
                                    quoted=quoted,
                                    guard=guard,
                                )
                            assets = await self._load_image_assets(
                                message_id, quoted=quoted, guard=guard
                            )
                            refreshed_high = sorted(
                                (
                                    a
                                    for a in assets
                                    if a.get("state") == "available"
                                    and _effective_variant(a) in {"original", "standard"}
                                    and a["id"] not in tried_ids
                                ),
                                key=lambda a: _VARIANT_RANK.get(_effective_variant(a), 99),
                            )
                            for asset in refreshed_high:
                                tried_ids.add(asset["id"])
                                try:
                                    return await self._download_validated_asset(
                                        asset, max_bytes=max_bytes, quoted=quoted, guard=guard
                                    )
                                except MediaError as error:
                                    if error.reason in {"recalled", "missing_scope"}:
                                        raise
                                    errors.append(error)
                        except MediaError as error:
                            if error.reason in {"recalled", "missing_scope"}:
                                raise
                            logger.warning(
                                "微信消息适配器 高清传输失败，尝试回退：message=%s reason=%s",
                                message_id,
                                error.reason,
                            )
                            errors.append(error)
                        break

                fallbacks = sorted(
                    (
                        a
                        for a in assets
                        if a.get("state") == "available" and a["id"] not in tried_ids
                    ),
                    key=lambda a: _VARIANT_RANK.get(_effective_variant(a), 99),
                )
                for asset in fallbacks:
                    tried_ids.add(asset["id"])
                    try:
                        return await self._download_validated_asset(
                            asset, max_bytes=max_bytes, quoted=quoted, guard=guard
                        )
                    except MediaError as error:
                        if error.reason in {"recalled", "missing_scope"}:
                            raise
                        errors.append(error)

                reasons = {e.reason for e in errors}
                for asset in assets:
                    state = asset.get("state")
                    code = asset.get("reason") or asset.get("errorCode")
                    if state == "recalled" or code == "message_recalled":
                        reasons.add("recalled")
                    elif state == "expired" or code == "media_expired":
                        reasons.add("expired")
                    elif state == "unsupported" or (
                        isinstance(code, str)
                        and _map_media_code(code, quoted=quoted).reason == "unsupported_format"
                    ):
                        reasons.add("unsupported_format")
                    elif isinstance(code, str):
                        reasons.add(_map_media_code(code, quoted=quoted).reason)
                for priority in (
                    "recalled",
                    "missing_scope",
                    "too_large",
                    "expired",
                    "unsupported_format",
                ):
                    if priority in reasons:
                        raise MediaError(priority)
                raise MediaError("unavailable")
        except TimeoutError:
            logger.warning(
                "微信消息适配器 图片读取超时：message=%s timeout_seconds=%s",
                message_id,
                self.media_timeout,
            )
            raise MediaError("unavailable") from None

    async def fetch_voice(self, message_id: str, *, guard=None) -> bytes:
        """Request Native's decoded normal WAV; never pass raw SILK to AstrBot ASR."""
        try:
            async with asyncio.timeout(self.media_timeout):
                assets = await self._load_assets(message_id, "voice", quoted=False, guard=guard)
                normal = [a for a in assets if _effective_variant(a) == "normal"]
                if not normal:
                    raise MediaError("unsupported_format", media_kind="voice")
                asset = next((a for a in normal if a.get("state") == "available"), normal[0])
                if asset.get("state") != "available":
                    state = asset.get("state")
                    if state in {"expired", "recalled", "unsupported", "unavailable", "failed"}:
                        reason = {
                            "expired": "expired",
                            "recalled": "recalled",
                            "unsupported": "unsupported_format",
                        }.get(state, "unavailable")
                        raise MediaError(reason, media_kind="voice")
                    if state not in {"pending", "queued", "running", "advertised"}:
                        raise MediaError("unavailable", media_kind="voice")
                    initial = None
                    job_id = asset.get("jobId")
                    if not isinstance(job_id, str) or not job_id:
                        _check_guard(guard)
                        initial = await self.transfer_media(
                            message_id, "normal", asset_id=asset["id"]
                        )
                        job_id = initial.get("id")
                    await self._poll_media_job(
                        job_id,
                        message_id=message_id,
                        initial=initial,
                        asset_id=asset["id"],
                        guard=guard,
                    )
                    refreshed = await self._load_assets(
                        message_id, "voice", quoted=False, guard=guard
                    )
                    current = next(
                        (
                            a
                            for a in refreshed
                            if a["id"] == asset["id"]
                            and _effective_variant(a) == "normal"
                            and a.get("state") == "available"
                        ),
                        None,
                    )
                    if current is None:
                        raise MediaError("unavailable", media_kind="voice")
                    asset = current
                size = asset.get("byteLength")
                sha = asset.get("sha256")
                if (
                    type(size) is not int
                    or size <= 0
                    or not isinstance(sha, str)
                    or not re.fullmatch(r"[a-fA-F0-9]{64}", sha)
                ):
                    raise MediaError("unavailable", media_kind="voice")
                if size > MAX_VOICE_BYTES:
                    raise MediaError("too_large", media_kind="voice")
                _check_guard(guard)
                data, content_type = await self.media_content(
                    asset["id"], max_bytes=MAX_VOICE_BYTES
                )
                _check_guard(guard)
                if len(data) != size or hashlib.sha256(data).hexdigest().lower() != sha.lower():
                    raise MediaError("unavailable", media_kind="voice")
                if content_type not in {"audio/wav", "audio/x-wav", "application/octet-stream"}:
                    raise MediaError("unsupported_format", media_kind="voice")
                validate_voice_bytes(data, normal=True)
                return data
        except NativeAPIError as error:
            raise _map_media_code(error.code, status=error.status, media_kind="voice") from None
        except MediaError as error:
            raise MediaError(error.reason, media_kind="voice") from None
        except TimeoutError:
            raise MediaError("unavailable", media_kind="voice") from None

    async def send(self, send_id: str):
        return await self._request("GET", self.prefix + "/sends/" + quote(send_id, safe=""))

    async def send_by_key(self, key: str):
        return await self._request(
            "GET", self.prefix + "/sends/by-idempotency-key", params={"key": key}
        )

    async def _submit_send(self, payload: dict, key: str) -> dict:
        try:
            return await self._request(
                "POST", self.prefix + "/sends", json=payload, headers={"Idempotency-Key": key}
            )
        except NativeAPIError as error:
            if 400 <= error.status < 500 and error.status not in {408, 429}:
                raise
            try:
                return await self.send_by_key(key)
            except NativeAPIError:
                raise SendError("submission_unknown", key) from None

    async def send_image(
        self,
        thread: str,
        data: bytes,
        content_type: str,
        key: str,
        *,
        record_job: Callable[[str], None],
        before_send: Callable[[], Awaitable[None]],
    ) -> dict:
        """Send an image message; Native supplies its original, standard and thumbnail copies."""
        suffix = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}
        if validate_image_bytes(data) != content_type:
            raise ValueError("发送图片 MIME 与内容不匹配")
        return await self._send_media(
            thread,
            data,
            content_type,
            key,
            kind="image",
            filename="image." + suffix[content_type],
            record_job=record_job,
            before_send=before_send,
        )

    async def send_video(
        self,
        thread: str,
        data: bytes,
        content_type: str,
        key: str,
        *,
        record_job: Callable[[str], None],
        before_send: Callable[[], Awaitable[None]],
    ) -> dict:
        if validate_video_bytes(data) != content_type:
            raise ValueError("发送视频 MIME 与内容不匹配")
        caps = await self.capabilities()
        limits = caps.get("mediaSendMaxBytes")
        limit = limits.get("video") if isinstance(limits, dict) else None
        upload_limit = caps.get("mediaUploadMaxBytes")
        if (
            type(limit) is not int
            or limit <= 0
            or type(upload_limit) is not int
            or upload_limit <= 0
        ):
            raise ValueError("Native 未提供视频发送能力，请检查服务版本和账号权限")
        if len(data) > min(limit, upload_limit):
            raise ValueError("视频超过 Native 服务允许的大小，未上传或发送")
        suffix = {
            "video/quicktime": "mov",
            "video/3gpp": "3gp",
            "video/3gpp2": "3g2",
            "video/mj2": "mj2",
        }.get(content_type, "mp4")
        receipt = await self._send_media(
            thread,
            data,
            content_type,
            key,
            kind="video",
            filename="video." + suffix,
            record_job=record_job,
            before_send=before_send,
        )
        result = receipt.get("result")
        prepared = result.get("videoPreparation") if isinstance(result, dict) else None
        if (
            isinstance(prepared, dict)
            and prepared.get("sourceSha256") == hashlib.sha256(data).hexdigest()
            and type(prepared.get("sourcePreserved")) is bool
            and type(prepared.get("normalBytes")) is int
            and 0 < prepared["normalBytes"] <= VIDEO_PROTOCOL_MAX_BYTES
        ):
            logger.info(
                "微信消息适配器 视频画质：source_bytes=%s playback_bytes=%s source_preserved=%s",
                len(data),
                prepared.get("normalBytes"),
                prepared.get("sourcePreserved") is True,
            )
        return receipt

    async def send_voice(
        self,
        thread: str,
        data: bytes,
        content_type: str,
        key: str,
        *,
        record_job: Callable[[str], None],
        before_send: Callable[[], Awaitable[None]],
        record_chunks: Callable[[list], None],
    ) -> dict:
        if validate_voice_bytes(data) != content_type:
            raise ValueError("发送语音 MIME 与内容不匹配")
        if content_type == "audio/wav":
            data = _normalize_streaming_wav(data)
        suffix = {
            "audio/wav": "wav",
            "audio/mpeg": "mp3",
            "audio/flac": "flac",
            "audio/ogg": "ogg",
            "audio/aac": "aac",
            "audio/mp4": "m4a",
            "audio/webm": "webm",
            "audio/silk": "silk",
        }
        return await self._send_media(
            thread,
            data,
            content_type,
            key,
            kind="voice",
            filename="voice." + suffix[content_type],
            record_job=record_job,
            before_send=before_send,
            record_chunks=record_chunks,
        )

    async def _send_media(
        self,
        thread: str,
        data: bytes,
        content_type: str,
        key: str,
        *,
        kind: str,
        filename: str,
        record_job: Callable[[str], None],
        before_send: Callable[[], Awaitable[None]],
        record_chunks=None,
    ) -> dict:
        """Share upload, idempotent submission and job polling for supported media."""
        form = aiohttp.FormData()
        form.add_field("file", data, filename=filename, content_type=content_type)
        options = (
            {"timeout": aiohttp.ClientTimeout(total=self.video_timeout)} if kind == "video" else {}
        )
        source = await self._request("POST", self.prefix + "/uploads", data=form, **options)
        if (
            not isinstance(source.get("id"), str)
            or not source["id"]
            or source.get("byteLength") != len(data)
            or source.get("sha256") != hashlib.sha256(data).hexdigest()
        ):
            raise SendError("upload_identity_mismatch", key)
        await before_send()
        value = await self._submit_send(
            {"threadId": thread, "kind": kind, "sourceId": source["id"]}, key
        )
        deadline = asyncio.get_running_loop().time() + (
            self.voice_send_timeout
            if kind == "voice"
            else self.video_send_timeout
            if kind == "video"
            else self.send_timeout
        )
        job_id = None
        server_id = None
        voice_chunks = []

        def inspection(state):
            return SendError(state, key, job_id, chunks=voice_chunks)

        while True:
            if (
                not isinstance(value.get("id"), str)
                or not value["id"]
                or (job_id is not None and value["id"] != job_id)
                or value.get("accountId") != self.account_id
                or value.get("threadId") != thread
                or value.get("kind") != "send"
                or value.get("sourceId") != source["id"]
                or (
                    kind != "voice"
                    and server_id is not None
                    and value.get("serverMessageId") != server_id
                )
            ):
                raise inspection("receipt_identity_mismatch")
            if job_id is None:
                job_id = value["id"]
                record_job(job_id)
            if value.get("serverMessageId") is not None:
                server_id = value["serverMessageId"]
            result = value.get("result")
            if kind == "voice" and isinstance(result, dict) and "chunks" in result:
                chunks = result["chunks"]
                if not isinstance(chunks, list) or len(chunks) > 256:
                    raise inspection("unknown")
                for index, chunk in enumerate(chunks):
                    if (
                        not isinstance(chunk, dict)
                        or chunk.get("index") != index
                        or chunk.get("state")
                        not in {"prepared", "submitting", "accepted", "rejected", "needs_review"}
                        or type(chunk.get("durationMs")) is not int
                        or not 0 < chunk["durationMs"] <= 60000
                        or (
                            chunk.get("serverMessageId") is not None
                            and (
                                not isinstance(chunk["serverMessageId"], str)
                                or not chunk["serverMessageId"]
                            )
                        )
                        or (chunk["state"] == "accepted" and not chunk.get("serverMessageId"))
                    ):
                        raise inspection("unknown")
                record_chunks(chunks)
                voice_chunks = chunks
            state = value.get("state")
            if state in {"failed", "cancelled", "partial", "needs_review"}:
                if state in {"failed", "cancelled"} and any(
                    c["state"] == "accepted" for c in voice_chunks
                ):
                    state = "partial"
                raise inspection(state)
            if state in {"accepted", "completed", "succeeded"}:
                result = value.get("result")
                if isinstance(result, dict):
                    if result.get("profileProjection") in {"unknown", "not_applicable"}:
                        raise inspection("needs_review")
                    if kind == "voice" and any(
                        c["state"] in {"rejected", "needs_review"} for c in voice_chunks
                    ):
                        raise inspection("needs_review")
                    ids = result.get("messageIds")
                    complete_voice = (
                        kind == "voice"
                        and bool(voice_chunks)
                        and isinstance(ids, list)
                        and len(ids) == len(voice_chunks)
                        and all(isinstance(item, str) and item for item in ids)
                        and len(set(ids)) == len(ids)
                        and all(chunk["state"] == "accepted" for chunk in voice_chunks)
                        and value.get("serverMessageId") == voice_chunks[-1]["serverMessageId"]
                    )
                    if complete_voice:
                        return value
                    if (
                        kind in {"image", "video"}
                        and isinstance(value.get("serverMessageId"), str)
                        and value["serverMessageId"]
                        and isinstance(ids, list)
                        and len(ids) == 1
                        and isinstance(ids[0], str)
                        and ids[0]
                    ):
                        return value
            elif state not in {"queued", "running", "submitting"}:
                raise inspection("unknown")
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise inspection("status_unavailable")
            try:
                async with asyncio.timeout(remaining):
                    await asyncio.sleep(min(self.poll_interval, remaining))
                    value = await self.job(job_id)
            except NativeAPIError as error:
                if error.status in {0, 408, 429} or 500 <= error.status < 600:
                    continue  # Retry reads only; the POST and upload are never repeated.
                raise inspection("status_unavailable") from None
            except TimeoutError:
                raise inspection("status_unavailable") from None

    async def _group_members_present(self, thread: str, user_ids: list[str]) -> bool:
        for username in user_ids:
            try:
                value = await self._request(
                    "GET",
                    self.prefix
                    + "/groups/"
                    + quote(thread, safe="")
                    + "/members/"
                    + quote(username, safe=""),
                )
            except NativeAPIError as error:
                if error.status == 404 and error.code in {"not_found", "media_not_cached"}:
                    return False
                raise
            if value.get("groupId") != thread or value.get("username") != username:
                raise ValueError("Native 群成员资料身份不匹配，未发送")
            if value.get("state") == "unconfirmed":
                return False
            member = value.get("member")
            if (
                value.get("state") != "present"
                or not isinstance(member, dict)
                or member.get("username") != username
            ):
                raise ValueError("Native 群成员资料无效，未发送")
        return True

    async def prepare_group_mentions(self, thread: str, user_ids: list[str]) -> None:
        """Refresh missing mention metadata before creating any send intent."""
        user_ids = list(dict.fromkeys(user_ids))
        operation_id = None
        try:
            async with asyncio.timeout(self.send_timeout):
                if await self._group_members_present(thread, user_ids):
                    return
                operation = await self._request(
                    "POST",
                    self.prefix + "/groups/" + quote(thread, safe="") + "/members/refresh",
                    json={},
                )
                operation_id = operation.get("id")
                while True:
                    if (
                        not isinstance(operation_id, str)
                        or not operation_id
                        or operation.get("id") != operation_id
                        or operation.get("accountId") != self.account_id
                        or operation.get("kind") != "metadata"
                    ):
                        raise ValueError("Native 群成员刷新回执身份不匹配，未发送")
                    state = operation.get("state")
                    if state == "succeeded":
                        break
                    if isinstance(state, str) and state in {"failed", "cancelled", "interrupted"}:
                        result = operation.get("result")
                        code = (
                            result.get("code") if isinstance(result, dict) else "operation_failed"
                        )
                        raise GroupMembersUnavailable(state, code, operation_id)
                    if not isinstance(state, str) or state not in {"queued", "running"}:
                        raise ValueError("Native 群成员刷新状态无效，未发送")
                    await asyncio.sleep(self.poll_interval)
                    operation = await self._request(
                        "GET", self.prefix + "/operations/" + quote(operation_id, safe="")
                    )
                if not await self._group_members_present(thread, user_ids):
                    raise GroupMembersUnavailable("succeeded", "members_unavailable", operation_id)
        except TimeoutError:
            raise GroupMembersUnavailable("timeout", "timeout", operation_id) from None

    async def send_text(
        self,
        thread: str,
        text: str,
        key: str,
        mentions: dict | None = None,
        *,
        quote: dict | None = None,
    ):
        if not text.strip() or len(text.encode("utf-8")) > 65536:
            raise ValueError("文本必须非空且不超过 64 KiB（含 @名称）")
        if quote is not None:
            if len(text.encode("utf-8")) > 16384:
                raise ValueError("Native 引用回复文本不能超过 16 KiB")
            if (
                not isinstance(quote, dict)
                or set(quote) != {"messageId", "serverId"}
                or any(not isinstance(value, str) or not value for value in quote.values())
            ):
                raise ValueError("Native 引用需要字符串 messageId 和 serverId")
        if not key or any(ord(x) < 33 or ord(x) > 126 for x in key):
            raise ValueError("无效 Idempotency-Key")
        if self.closed:
            raise ValueError("Native 客户端未连接，未提交发送")
        payload = {"threadId": thread, "text": text}
        if mentions:
            payload["mentions"] = mentions
        if quote is not None:
            payload["quote"] = quote
        value = await self._submit_send(payload, key)
        return await self._wait_app_send(value, thread, key)

    async def prepare_forward(self, nodes):
        from .forward import prepare_forward_nodes

        async def upload(image):
            data, content_type = await load_outgoing_image(image)
            form = aiohttp.FormData()
            suffix = {
                "image/jpeg": "jpg",
                "image/png": "png",
                "image/gif": "gif",
                "image/webp": "webp",
            }
            form.add_field(
                "file", data, filename="image." + suffix[content_type], content_type=content_type
            )
            source = await self._request("POST", self.prefix + "/uploads", data=form)
            if (
                not isinstance(source.get("id"), str)
                or not source["id"]
                or source.get("byteLength") != len(data)
                or source.get("sha256") != hashlib.sha256(data).hexdigest()
            ):
                raise ValueError("合并转发图片上传校验失败，未提交发送")
            return source["id"]

        return await prepare_forward_nodes(nodes, upload)

    async def send_forward(self, thread, nodes, key, *, record_send):
        value = await self._submit_send(
            {"threadId": thread, "kind": "forward", "nodes": nodes}, key
        )
        return await self._wait_app_send(
            value, thread, key, record_send=record_send, read_budget=300
        )

    async def _wait_app_send(self, value, thread, key, *, record_send=None, read_budget=None):
        deadline = asyncio.get_running_loop().time() + (read_budget or self.send_timeout)
        send_id = None
        poll_failed = False
        while True:
            if (
                not isinstance(value.get("id"), str)
                or not value["id"]
                or value.get("accountId") != self.account_id
                or value.get("destination") != thread
                or (send_id and send_id != value["id"])
            ):
                raise SendError("receipt_identity_mismatch", key, send_id)
            send_id = value["id"]
            if record_send is not None:
                record_send(send_id)
            state = value.get("state")
            if state == "accepted":
                return value  # Server acceptance, not proof of recipient delivery.
            if state not in {"queued", "submitting"}:
                safe_state = (
                    state if state in {"needs_review", "failed", "cancelled"} else "unknown"
                )
                raise SendError(safe_state, key, send_id)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise SendError("status_unavailable" if poll_failed else state, key, send_id)
            await asyncio.sleep(min(self.poll_interval, remaining))
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise SendError("status_unavailable" if poll_failed else state, key, send_id)
            try:
                async with asyncio.timeout(remaining):
                    value = await self.send(send_id)
                poll_failed = False
            except NativeAPIError as error:
                if error.status in {0, 408, 429} or 500 <= error.status < 600:
                    poll_failed = True
                    continue  # Retry only the read, with the same send ID and deadline.
                raise SendError("status_unavailable", key, send_id) from None
            except TimeoutError:
                raise SendError(
                    "status_unavailable" if poll_failed else state, key, send_id
                ) from None

    async def events(self, after: str) -> AsyncIterator[dict]:
        cursor_number(after)
        if self.closed:
            raise NativeAPIError(0)
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=self.request_timeout, sock_read=self.idle_timeout
        )
        try:
            async with self._session.get(
                self.api_url + self.prefix + "/events/stream",
                params={"after": after},
                headers={"Accept": "text/event-stream", "Last-Event-ID": after},
                allow_redirects=False,
                timeout=timeout,
                read_bufsize=512 * 1024,  # StreamReader's line high-water limit is twice this.
            ) as response:
                if response.status != 200:
                    raise NativeAPIError(
                        response.status, "event_cursor_expired" if response.status == 410 else ""
                    )
                if response.content_type != "text/event-stream":
                    raise NativeAPIError(502)
                lines, event_type, event_id, size = [], "", "", 0
                async for raw in response.content:
                    try:
                        line = raw.decode("utf-8").rstrip("\r\n")
                    except UnicodeDecodeError:
                        raise NativeAPIError(502) from None
                    if line.startswith(":"):
                        continue  # Comments are not part of the pending event frame.
                    size += len(raw)
                    if size > 1024 * 1024:
                        raise NativeAPIError(502)
                    if not line:
                        if lines:
                            try:
                                value = json.loads("\n".join(lines))
                                if not isinstance(value, dict):
                                    raise ValueError
                                if event_type == "error":
                                    code = value.get("error", {}).get("code")
                                    raise NativeAPIError(
                                        410 if code == "event_cursor_expired" else 502, code
                                    )
                                cursor = value.get("cursor", event_id)
                                cursor_number(cursor)
                                if event_id and cursor != event_id:
                                    raise ValueError
                                if not isinstance(value.get("kind"), str) or not isinstance(
                                    value.get("data"), dict
                                ):
                                    raise ValueError
                                value["cursor"] = cursor
                            except (ValueError, AttributeError, TypeError):
                                raise NativeAPIError(502) from None
                            yield value
                        lines, event_type, event_id, size = [], "", "", 0
                    else:
                        field, _, data = line.partition(":")
                        if data.startswith(" "):
                            data = data[1:]
                        if field == "data":
                            lines.append(data)
                        elif field == "event":
                            event_type = data
                        elif field == "id":
                            event_id = data
                # An incomplete frame must be replayed from the last committed cursor.
        except (ValueError, LineTooLong):
            raise NativeAPIError(502) from None
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise NativeAPIError(0, "timeout") from None
