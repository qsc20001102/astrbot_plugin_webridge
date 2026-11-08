"""Check the real login contract offline, in a new account and network namespace."""

import asyncio
import importlib
import json
import os
import socket
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


async def main():
    if os.environ.get("WEBRIDGE_OFFLINE_LOGIN_SMOKE") != "1" or any(
        name != "lo" for _, name in socket.if_nameindex()
    ):
        raise RuntimeError("Run only in an isolated container with --network none")
    package = types.ModuleType("webridge_login_smoke")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    runtime_module = importlib.import_module("webridge_login_smoke.runtime")
    ui = importlib.import_module("webridge_login_smoke.native_ui")
    with tempfile.TemporaryDirectory(prefix="webridge-login-contract-") as temporary:
        engine = runtime_module.NativeRuntime(Path(temporary))
        try:
            await engine.start()
            await engine.client._request("POST", "/accounts", json={"id": "offline-login"})
            async with await engine.account_client("offline-login") as client:
                key = "offline-login-state-contract"
                first = await client.login(key)
                assert first["state"] == "starting", first["state"]
                replay = await client.login(key)
                assert replay["id"] == first["id"]
                engine.login_attempts["offline-login"] = {"key": key, "id": first["id"]}
                view = ui.NativeView(SimpleNamespace(get_config=lambda: {"platform": []}), engine)
                result = await view._operation(client, "offline-login", first["id"])
                assert result["terminal"] == (result["state"] in ui.TERMINAL_LOGIN_STATES)
                if not result["terminal"]:
                    assert engine.login_attempts["offline-login"]["id"] == first["id"]
                print(
                    json.dumps(
                        {
                            "real_native_initial_state": first["state"],
                            "snapshot_state": result["state"],
                            "terminal_classification": "passed",
                            "idempotent_replay": "same operation",
                            "network_isolated": True,
                            "wechat_contact_or_messages": False,
                        }
                    )
                )
        finally:
            await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
