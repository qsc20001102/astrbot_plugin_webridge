"""Run in an isolated Linux AstrBot image; no WeChat login or message is attempted."""

import asyncio
import importlib
import json
import sys
import tempfile
import types
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


async def main():
    package = types.ModuleType("webridge_smoke")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    runtime_module = importlib.import_module("webridge_smoke.runtime")
    with tempfile.TemporaryDirectory(prefix="webridge-native-smoke-") as temporary:
        engine = runtime_module.NativeRuntime(Path(temporary))
        runtime_module._instance = engine
        manager = None
        try:
            await asyncio.gather(*(engine.start() for _ in range(4)))
            first = await engine.client.status()
            assert engine.ready and first["pid"] == engine.process.pid
            assert first["version"]["target"] == runtime_module.TARGET
            assert await engine.accounts() == []
            from astrbot.api.star import Context
            from astrbot.api.web import PluginRequest, bind_request_context
            from astrbot.core.config.astrbot_config import AstrBotConfig
            from astrbot.core.platform.manager import PlatformManager
            from starlette.requests import Request

            importlib.import_module("webridge_smoke.adapter")
            config = AstrBotConfig(str(Path(temporary) / "astrbot.json"))
            config["platform"] = []
            manager = PlatformManager(config, asyncio.Queue())
            context = Context.__new__(Context)
            context.platform_manager = manager
            context.get_config = lambda: config
            view = importlib.import_module("webridge_smoke.native_ui").NativeView(context, engine)

            async def invoke(method, data):
                async def receive():
                    return {"type": "http.request", "body": json.dumps(data).encode()}

                req = Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": "/smoke",
                        "query_string": b"",
                        "headers": [],
                    },
                    receive,
                )
                with bind_request_context(PluginRequest(req)):
                    return json.loads((await getattr(view, method)()).body)

            added = await invoke(
                "create_account", {"request_id": str(uuid4()), "alias": "验证账号"}
            )
            assert added["ok"] is True
            account = added["account_id"]
            accounts = await engine.accounts()
            assert len(accounts) == 1 and accounts[0]["id"] == account
            assert accounts[0]["authenticated"] is False
            native_config = await engine.connection(account)
            assert native_config.api_url.startswith("http://127.0.0.1:")
            assert native_config.account_id == account
            async with await engine.account_client(account) as client:
                assert (await client.account())["id"] == account
            assert (await invoke("platform", {"account_id": account}))["ok"] is False
            platform = {
                "id": "webridge-smoke",
                "type": "webridge",
                "enable": True,
                "account_id": account,
            }
            config["platform"].append(platform)
            await manager.reload(platform)
            assert context.get_platform_inst("webridge-smoke").meta().name == "webridge"
            await asyncio.sleep(0.2)
            await manager.terminate()
            old_process = engine.process
            await engine.stop()
            assert old_process.returncode == 0 and not engine.ready
            await engine.start()
            second = await engine.client.status()
            assert second["serverId"] == first["serverId"]
            assert second["ownershipGeneration"] != first["ownershipGeneration"]
            assert engine._token != native_config.token
            assert [a["id"] for a in await engine.accounts()] == [account]
            assert engine.account_labels()[account] == "验证账号"
            print(
                json.dumps(
                    {
                        "runtime": "passed",
                        "version": engine.version,
                        "target": runtime_module.TARGET,
                        "concurrent_start": "one process",
                        "private_listener": True,
                        "account_creation": True,
                        "profile_survives_restart": True,
                        "token_rotates": True,
                        "real_astrbot_platform_lifecycle": True,
                        "real_wechat_login_or_send": False,
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            if manager is not None:
                await manager.terminate()
            await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
