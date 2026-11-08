"""Owned runtime admission, shutdown and automatic account selection."""

import hashlib
import io
import json
from types import SimpleNamespace

import pytest
from conftest import module


def artifact(tmp_path, monkeypatch):
    runtime = module("runtime")
    monkeypatch.setattr(runtime, "check_host", lambda: None)
    path = tmp_path / "bin/linux-x86_64/wx"
    path.parent.mkdir(parents=True)
    data = b"\x7fELF\x02\x01" + b"\0" * 12 + b"\x3e\x00" + b"test-only ELF header"
    path.write_bytes(data)
    manifest = {
        "target": runtime.TARGET,
        "upstream_commit": runtime.UPSTREAM_COMMIT,
        "version": "1.5.1",
        "image_original": True,
        "video_passthrough": True,
        "video_size_policy": "protocol_only",
        "incoming_video_download": False,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    (tmp_path / "native").mkdir()
    (tmp_path / "native/manifest.json").write_text(json.dumps(manifest))
    return runtime, path, manifest


@pytest.mark.parametrize(
    "system,machine", [("Windows", "AMD64"), ("Linux", "aarch64"), ("Linux", "i686")]
)
def test_only_linux_x86_64_is_admitted(monkeypatch, system, machine):
    runtime = module("runtime")
    monkeypatch.setattr(runtime.platform, "system", lambda: system)
    monkeypatch.setattr(runtime.platform, "machine", lambda: machine)
    with pytest.raises(runtime.RuntimeUnavailable, match="Linux x86_64"):
        runtime.check_host()


def test_manifest_binds_binary_and_image_fix_to_exact_upstream(tmp_path, monkeypatch):
    runtime, binary, manifest = artifact(tmp_path, monkeypatch)
    assert runtime.verify_executable(tmp_path) == (binary, manifest)
    binary.write_bytes(binary.read_bytes() + b"modified")
    with pytest.raises(runtime.RuntimeUnavailable, match="校验失败"):
        runtime.verify_executable(tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        {"image_original": False},
        {"video_passthrough": False},
        {"video_size_policy": "legacy"},
        {"incoming_video_download": True},
        {"target": "aarch64-unknown-linux-gnu"},
        {"upstream_commit": "0" * 40},
    ],
)
def test_unpatched_or_wrong_target_binary_is_rejected(tmp_path, monkeypatch, change):
    runtime, _, manifest = artifact(tmp_path, monkeypatch)
    manifest.update(change)
    (tmp_path / "native/manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(runtime.RuntimeUnavailable):
        runtime.verify_executable(tmp_path)


async def test_connection_auto_selects_only_a_single_authenticated_account(tmp_path, monkeypatch):
    runtime = module("runtime").NativeRuntime(tmp_path)
    runtime.api_url, runtime._token = "http://127.0.0.1:12345/v1", "internal-only-token"
    accounts = []

    async def listed():
        return accounts

    monkeypatch.setattr(runtime, "accounts", listed)
    with pytest.raises(module("runtime").RuntimeUnavailable):
        await runtime.connection()
    accounts.append({"id": "one", "authenticated": True})
    config = await runtime.connection()
    assert config.account_id == "one" and config.token == "internal-only-token"
    assert "internal-only-token" not in repr(config)
    accounts.append({"id": "two", "authenticated": True})
    with pytest.raises(module("runtime").RuntimeUnavailable):
        await runtime.connection()
    assert (await runtime.connection("two")).account_id == "two"
    with pytest.raises(module("runtime").RuntimeUnavailable):
        await runtime.connection("foreign")


async def test_shutdown_is_scoped_to_owned_process_and_retains_login_files(tmp_path):
    runtime = module("runtime").NativeRuntime(tmp_path)
    runtime.data_dir.mkdir()
    saved = runtime.data_dir / "owned-profile-marker"
    saved.write_text("retain this profile")
    calls = []
    process = SimpleNamespace(returncode=None)

    async def wait():
        process.returncode = 0
        return 0

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"state": "stopping"}

    async def close():
        calls.append("closed")

    process.wait = wait
    runtime.process = process
    runtime.identity = {
        "pid": 123,
        "serverId": "server-one",
        "ownershipGeneration": "generation-one",
    }
    runtime.client = SimpleNamespace(_request=request, close=close)
    lock = runtime._file_lock = io.BytesIO()
    await runtime.stop()
    assert calls[0] == (
        "POST",
        "/daemon/shutdown",
        {"json": {"pid": 123, "serverId": "server-one", "ownershipGeneration": "generation-one"}},
    )
    assert calls[1] == "closed" and lock.closed
    assert runtime.process is runtime.client is None
    assert saved.read_text() == "retain this profile"
