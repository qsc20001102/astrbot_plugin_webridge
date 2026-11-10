"""Plugin-owned Native process. Its listener and bootstrap token stay private."""

import asyncio
import hashlib
import json
import os
import platform
import secrets
import socket
from contextlib import suppress
from pathlib import Path

from astrbot.api.star import StarTools

from .config import PLUGIN_ID, NativeConfig
from .native_client import NativeAPIError, NativeClient

ROOT = Path(__file__).resolve().parent
TARGET = "x86_64-unknown-linux-gnu"
UPSTREAM_COMMIT = "39af1eec84f4763e1257406f8c5fb848b31d82e9"


class RuntimeUnavailable(RuntimeError):
    """Safe, actionable text for the authenticated plugin UI."""


def check_host() -> None:
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeUnavailable("内置微信引擎仅支持 Linux x86_64（amd64）")
    libc, version = platform.libc_ver()
    if libc == "glibc" and version:
        try:
            if tuple(map(int, version.split(".")[:2])) < (2, 36):
                raise RuntimeUnavailable(
                    "当前系统的 glibc 过旧，请使用 Debian 12 或更新的 AstrBot 镜像"
                )
        except ValueError:
            raise RuntimeUnavailable("无法确认系统运行库版本") from None


def verify_executable(root: Path = ROOT) -> tuple[Path, dict]:
    check_host()
    try:
        manifest = json.loads((root / "native/manifest.json").read_text(encoding="utf-8"))
        binary = root / "bin/linux-x86_64/wx"
        if (
            manifest.get("target") != TARGET
            or manifest.get("upstream_commit") != UPSTREAM_COMMIT
            or manifest.get("image_original") is not True
            or manifest.get("video_passthrough") is not True
            or manifest.get("video_size_policy") != "protocol_only"
            or manifest.get("incoming_video_download") is not False
            or manifest.get("merged_forward") is not True
            or manifest.get("forward_projection") != "xml-v1"
            or manifest.get("temporary_media_sources") is not True
            or binary.is_symlink()
            or not binary.is_file()
        ):
            raise ValueError
        with binary.open("rb") as stream:
            header = stream.read(20)
            stream.seek(0)
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if (
            header[:6] != b"\x7fELF\x02\x01"
            or header[18:20] != b"\x3e\x00"
            or actual != manifest.get("sha256")
        ):
            raise ValueError
        if not os.access(binary, os.X_OK):
            binary.chmod(binary.stat().st_mode | 0o500)
        return binary, manifest
    except (OSError, ValueError, TypeError):
        raise RuntimeUnavailable(
            "内置引擎缺失或校验失败，请安装完整的 Linux x86_64 发行包"
        ) from None


class NativeRuntime:
    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir or StarTools.get_data_dir(PLUGIN_ID)) / "native"
        self.process = None
        self.client = None
        self.api_url = ""
        self._token = ""
        self.identity = None
        self.version = ""
        self.reason = ""
        self._lock = asyncio.Lock()
        self._stderr_task = None
        self._file_lock = None
        self.login_attempts = {}
        self.account_locks = {}
        self._labels_lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return bool(
            self.process is not None
            and self.process.returncode is None
            and self.identity is not None
            and self.client is not None
            and not self.client.closed
        )

    def account_lock(self, account_id: str) -> asyncio.Lock:
        return self.account_locks.setdefault(account_id, asyncio.Lock())

    def account_labels(self) -> dict:
        path = self.data_dir.parent / "account-labels.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and all(
                isinstance(k, str) and isinstance(v, str) for k, v in value.items()
            ):
                return value
        except (OSError, ValueError):
            pass
        return {}

    async def set_account_label(self, account: str, label: str):
        # Native aliases accept ASCII only. Keep human-readable Chinese names
        # in plugin-owned metadata; never change the Native profile schema.
        async with self._labels_lock:

            def write():
                labels = self.account_labels()
                labels[account] = label
                path = self.data_dir.parent / "account-labels.json"
                temporary = path.with_suffix(".tmp")
                with temporary.open("w", encoding="utf-8") as stream:
                    json.dump(labels, stream, ensure_ascii=False)
                temporary.chmod(0o600)
                temporary.replace(path)

            await asyncio.to_thread(write)

    async def start(self) -> None:
        async with self._lock:
            if self.ready:
                return
            await self._stop()
            try:
                binary, manifest = await asyncio.to_thread(verify_executable)
                await asyncio.to_thread(
                    self.data_dir.mkdir, parents=True, exist_ok=True, mode=0o700
                )
                await asyncio.to_thread(self.data_dir.chmod, 0o700)
                # Linux-only at the actual process boundary, so protocol tests
                # can still run on a development host using an injected engine.
                import fcntl

                self._file_lock = (self.data_dir / "webridge.lock").open("a+b")
                try:
                    fcntl.flock(self._file_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise RuntimeUnavailable("另一个微信消息适配器进程正在使用此登录目录") from None
                environment = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.upper().startswith(("WECHAT_", "WX_", "AGENT_WECHAT_"))
                }
                environment["HOME"] = str(self.data_dir.resolve())
                # Never pass the bootstrap token in argv, config files or logs.
                self._token = secrets.token_urlsafe(48)
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                self.api_url = f"http://127.0.0.1:{port}/v1"
                self.process = await asyncio.create_subprocess_exec(
                    str(binary),
                    "serve",
                    "--data-dir",
                    str(self.data_dir.resolve()),
                    "--token-stdin",
                    "--listen",
                    f"127.0.0.1:{port}",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env=environment,
                )
                self._stderr_task = asyncio.create_task(self._drain_stderr())
                self.process.stdin.write((self._token + "\n").encode("ascii"))
                await self.process.stdin.drain()
                self.process.stdin.close()
                self.client = NativeClient(self.api_url, "", self._token, request_timeout=2)
                await self.client.open()
                async with asyncio.timeout(20):
                    while True:
                        if self.process.returncode is not None:
                            raise RuntimeUnavailable("微信引擎启动失败，请检查完整发行包和系统依赖")
                        try:
                            status = await self.client.status()
                            version = status.get("version")
                            if (
                                not isinstance(version, dict)
                                or version.get("backend") != "native"
                                or version.get("apiVersion") != 1
                                or version.get("target") != TARGET
                                or version.get("version") != manifest.get("version")
                                or status.get("pid") != self.process.pid
                                or status.get("dataDir") != str(self.data_dir.resolve())
                                or not isinstance(status.get("serverId"), str)
                                or not status["serverId"]
                                or not isinstance(status.get("ownershipGeneration"), str)
                                or not status["ownershipGeneration"]
                            ):
                                raise RuntimeUnavailable(
                                    "微信引擎身份不匹配，已停止本插件创建的进程"
                                )
                            if status.get("state") == "ready":
                                self.identity = {
                                    key: status[key]
                                    for key in ("pid", "serverId", "ownershipGeneration")
                                }
                                self.version = version["version"]
                                self.reason = ""
                                return
                        except NativeAPIError:
                            pass
                        await asyncio.sleep(0.1)
            except BaseException as error:
                self.reason = (
                    str(error)
                    if isinstance(error, RuntimeUnavailable)
                    else "微信引擎启动失败或超时"
                )
                await self._stop()
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise RuntimeUnavailable(self.reason) from None

    async def _drain_stderr(self):
        # Native diagnostics can contain account/protocol details. Consume them
        # without forwarding them to AstrBot logs or retaining their contents.
        while await self.process.stderr.read(4096):
            pass

    async def _stop(self):
        process, client = self.process, self.client
        if process is not None and process.returncode is None:
            if self.identity is not None and client is not None:
                with suppress(NativeAPIError, TimeoutError):
                    async with asyncio.timeout(3):
                        await client._request("POST", "/daemon/shutdown", json=self.identity)
            try:
                await asyncio.wait_for(process.wait(), 5 if self.identity else 0.1)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
        if client is not None:
            await client.close()
        if self._file_lock is not None:
            self._file_lock.close()
        self.process = self.client = self.identity = self._file_lock = None
        self._stderr_task = None
        self._token = ""
        self.api_url = ""

    async def stop(self):
        async with self._lock:
            await self._stop()

    async def accounts(self) -> list[dict]:
        await self.start()
        response = await self.client.accounts()
        items = response.get("items")
        if not isinstance(items, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
            for item in items
        ):
            raise RuntimeUnavailable("微信引擎返回的账号列表无效")
        return items

    async def connection(self, account_id: str = "") -> NativeConfig:
        accounts = await self.accounts()
        if not account_id:
            authenticated = [item for item in accounts if item.get("authenticated") is True]
            if len(authenticated) != 1:
                raise RuntimeUnavailable(
                    "请先在微信消息适配器界面登录微信；多个账号时请选择要接入的账号"
                )
            account_id = authenticated[0]["id"]
        if not any(item["id"] == account_id for item in accounts):
            raise RuntimeUnavailable("所选微信账号不存在，请在微信消息适配器界面重新选择")
        return NativeConfig(self.api_url, account_id, self._token, "")

    async def account_client(self, account_id: str) -> NativeClient:
        config = await self.connection(account_id)
        client = NativeClient(config.api_url, config.account_id, config.token, request_timeout=10)
        await client.open()
        return client


_instance = None


def get_runtime() -> NativeRuntime:
    global _instance
    if _instance is None:
        _instance = NativeRuntime()
    return _instance


async def close_runtime() -> None:
    global _instance
    if _instance is not None:
        await _instance.stop()
        _instance = None
