"""Real AstrBot platform manager: plugin reload replaces saved stale instances."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.core.platform.manager import PlatformManager
from astrbot.core.platform.sources.webchat.webchat_adapter import WebChatAdapter
from conftest import module
from test_connection_view import plugin


async def test_hot_reload_recreates_saved_bot_once_and_unload_stops_only_webridge(monkeypatch):
    main = module("main")
    instance, context = plugin(
        [{"id": "managed", "type": "webridge", "enable": True, "account_id": "one"}]
    )
    context._config["platform_settings"] = {}
    manager = PlatformManager(context._config, asyncio.Queue())
    context.platform_manager = manager
    webchat = WebChatAdapter({}, {}, asyncio.Queue())
    manager.platform_insts.append(webchat)
    engine = SimpleNamespace(start=AsyncMock())
    monkeypatch.setattr(main, "get_runtime", lambda: engine)
    stopped = AsyncMock()
    monkeypatch.setattr(main, "close_runtime", stopped)
    try:
        await instance.initialize()
        active = context.get_platform_inst("managed")
        assert type(active) is main.WeBridgeAdapter
        await instance.initialize()
        assert context.get_platform_inst("managed") is active
        assert len(manager.get_insts()) == 2
        await instance.terminate()
        assert active.stopped and context.get_platform_inst("managed") is None
        assert manager.get_insts() == [webchat]
        stopped.assert_awaited_once()
    finally:
        await manager.terminate()
