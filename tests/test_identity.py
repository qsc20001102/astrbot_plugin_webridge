"""WeBridge is a separate platform and never adopts another plugin's data."""

import asyncio
from copy import deepcopy

from astrbot.core.event_bus import UNIQUE_SESSION_ID_BUILDERS
from astrbot.core.platform.register import platform_cls_map
from conftest import module
from test_connection_view import api, plugin, saved_robot


async def test_new_platform_starts_empty_next_to_old_state_and_persists_its_own(
    tmp_path, monkeypatch
):
    adapter_module = module("adapter")
    config = module("config")
    cfg = {
        "id": "independent-bot",
        "type": "webridge",
        "server_url": "http://native.example:6174",
        "account_id": "same-native-account",
        "token": "synthetic-test-token",
    }
    old_dir = tmp_path / "plugin_data" / "astrbot_plugin_wechat_native"
    old = adapter_module.WeBridgeAdapter(cfg, {}, asyncio.Queue(), state_dir=old_dir)
    old.state.accept("42", "old-message", "peer", "old-server", "replied")
    old.state.begin_send("old-key", "old-message", "peer")
    old.state.finish_send("old-key", "accepted", "old-send")
    await old.terminate()
    old_path = (await asyncio.to_thread(lambda: list(old_dir.glob("*.sqlite3"))))[0]
    original_bytes = await asyncio.to_thread(old_path.read_bytes)
    requested_namespaces = []

    def data_dir(name):
        requested_namespaces.append(name)
        return tmp_path / "plugin_data" / name

    monkeypatch.setattr(adapter_module.StarTools, "get_data_dir", data_dir)
    fresh = adapter_module.WeBridgeAdapter(cfg, {}, asyncio.Queue())
    try:
        assert fresh.meta().name == "webridge"
        assert fresh.state.cursor == "0"
        assert fresh.state.message_status("old-message") is None
        assert fresh.state.send_record("old-key") is None
        fresh.state.advance("7")
    finally:
        await fresh.terminate()
    restarted = adapter_module.WeBridgeAdapter(cfg, {}, asyncio.Queue())
    try:
        assert restarted.state.cursor == "7"
    finally:
        await restarted.terminate()
    assert requested_namespaces == [config.PLUGIN_ID, config.PLUGIN_ID]
    assert config.PLUGIN_ID == "astrbot_plugin_webridge"
    assert await asyncio.to_thread(old_path.read_bytes) == original_bytes


async def test_old_robot_config_is_excluded_and_never_migrated():
    robots = [
        saved_robot(
            "http://old.example",
            type="wechat_native",
            id="old-bot",
            allowed_users=["old-user"],
            enable_group_summary=True,
        ),
        saved_robot("http://new.example", id="new-bridge"),
    ]
    before = deepcopy(robots)
    instance, context = plugin(robots)
    await instance.initialize()
    try:
        assert await api(context, "bots") == {"bots": [{"id": "new-bridge"}]}
        assert context.get_config()["platform"] == before
        assert robots == before
    finally:
        await instance.terminate()


async def test_platform_registration_and_sessions_do_not_replace_the_old_plugin(monkeypatch):
    def original_builder(event):
        return "original-session"

    class OriginalPlatform:
        pass

    monkeypatch.setitem(UNIQUE_SESSION_ID_BUILDERS, "wechat_native", original_builder)
    monkeypatch.setitem(platform_cls_map, "wechat_native", OriginalPlatform)
    instance, _ = plugin([])
    await instance.initialize()
    try:
        assert platform_cls_map["webridge"] is module("adapter").WeBridgeAdapter
        assert platform_cls_map["wechat_native"] is OriginalPlatform
        assert UNIQUE_SESSION_ID_BUILDERS["webridge"] is module("main").webridge_session_builder
        assert UNIQUE_SESSION_ID_BUILDERS["wechat_native"] is original_builder
    finally:
        await instance.terminate()
    assert "webridge" not in UNIQUE_SESSION_ID_BUILDERS
    assert UNIQUE_SESSION_ID_BUILDERS["wechat_native"] is original_builder
