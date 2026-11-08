"""Read-only connection checks for the AstrBot Plugin View."""

import asyncio

from astrbot.api.web import json_response, request

from .config import ADAPTER_ID, PLUGIN_DISPLAY_NAME, NativeConfig
from .native_client import NativeAPIError, NativeClient
from .runtime import RuntimeUnavailable, get_runtime


def _json(data):
    return json_response(data, headers={"Cache-Control": "no-store"})


def _result(reason="", online=None):
    return _json(
        {
            "state": "failed" if online is None else "connected",
            "wechat_online": online,
            "reason": reason,
        }
    )


class ConnectionView:
    def __init__(self, context):
        self.context = context

    def _robots(self):
        # Read saved settings, including disabled/unstartable robots. Never use
        # the adapter constructor here: it opens the persistent message journal.
        return [
            robot
            for robot in self.context.get_config().get("platform", [])
            if isinstance(robot, dict)
            and robot.get("type") == ADAPTER_ID
            and isinstance(robot.get("id"), str)
            and robot["id"]
        ]

    async def bots(self):
        return _json({"bots": [{"id": robot["id"]} for robot in self._robots()]})

    async def check(self):
        bot_id = request.query.get("bot_id", "")
        robot = next((robot for robot in self._robots() if robot["id"] == bot_id), None)
        if robot is None:
            return _result(f"请选择已保存的 {PLUGIN_DISPLAY_NAME} 机器人")
        try:
            config = (
                NativeConfig.from_dict(robot)
                if robot.get("server_url")
                else await get_runtime().connection(robot.get("account_id", ""))
            )
            token = await asyncio.to_thread(config.load_token)
        except RuntimeUnavailable as error:
            return _result(str(error))
        except (TypeError, ValueError):
            return _result("连接配置无效，请检查地址、Account ID 和 Token 配置")
        try:
            async with NativeClient(
                config.api_url, config.account_id, token, request_timeout=5
            ) as client:
                status = await client.status()
                if not isinstance(status.get("state"), str):
                    return _result("Native 服务响应无效")
                if status["state"] != "ready":
                    return _result("Native 服务尚未就绪")
                account = await client.account()
                if (
                    not isinstance(account.get("id"), str)
                    or not isinstance(account.get("authenticated"), bool)
                    or not isinstance(account.get("connection"), str)
                ):
                    return _result("Native 账号响应无效")
                if account["id"] != config.account_id:
                    return _result("Account ID 与 Native 返回的账号不一致")
                if not account["authenticated"]:
                    return _result("微信账号尚未登录", False)
                if account["connection"] != "online":
                    return _result("微信账号当前不在线", False)
                return _result(online=True)
        except NativeAPIError as error:
            # Fixed public strings only: no URLs, token paths, response bodies,
            # account details or underlying exception text reach the browser.
            reasons = {
                0: "无法连接 Native 服务或请求超时",
                401: "Native Token 无效或已过期",
                403: "Native Token 无权读取服务或该账号",
                404: "Native API 地址或 Account ID 不存在",
                502: "Native 服务响应无效",
            }
            return _result(reasons.get(error.status, "Native 服务暂时不可用"))
