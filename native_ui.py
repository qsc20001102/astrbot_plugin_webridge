"""Authenticated Plugin View API for the private engine and WeChat accounts."""

import asyncio
import base64
import hashlib
from functools import wraps
from uuid import UUID, uuid4

from astrbot.api import logger
from astrbot.api.web import json_response, request

from .config import ADAPTER_ID, DEFAULT_CONFIG
from .native_client import NativeAPIError
from .runtime import RuntimeUnavailable

# Native v1.5.1 starts at "starting", then may resume or authenticate before
# completing. Only the terminal states in its operation store end an attempt.
TERMINAL_LOGIN_STATES = {
    "cancelled",
    "succeeded",
    "failed",
    "expired",
    "rejected",
    "needs_login",
    "needs_review",
}
LOGIN_FAILURE_REASONS = {
    "login_failed": "微信登录未完成，请检查服务器网络或插件日志后重试",
    "login_lan_challenge_failed": "手机确认未完成，请检查手机与服务器的局域网连通后重新扫码",
    "login_poll_failed": "微信登录状态查询失败，请检查服务器网络后重新获取二维码",
    "operation_interrupted": "微信引擎重启中断了本次登录，请重新获取二维码",
    "auth_required": "微信登录凭据已失效，请重新扫码",
    "account_identity_mismatch": "微信账号与已有登录资料不一致，请检查所选账号",
    "account_device_mismatch": "微信设备与已有登录资料不一致，请检查所选账号",
    "profile_keys_unavailable": "本地微信登录资料暂不可用，请检查插件日志",
}


def response(data):
    return json_response(data, headers={"Cache-Control": "no-store"})


def guarded(method):
    @wraps(method)
    async def invoke(self, *args, **kwargs):
        try:
            return response({"ok": True, **await method(self, *args, **kwargs)})
        except (RuntimeUnavailable, ValueError) as error:
            return response({"ok": False, "reason": str(error)})
        except NativeAPIError as error:
            logger.warning(
                "微信账号操作失败：action=%s status=%s code=%s",
                method.__name__,
                error.status,
                error.code,
            )
            return response(
                {
                    "ok": False,
                    "reason": {
                        "operation_in_progress": "已有微信登录或退出操作正在进行，请稍后重试；"
                        "持续无法恢复时请重启插件",
                        "idempotency_conflict": "登录请求状态冲突，请重启插件后重新获取二维码",
                        "account_sign_out_required": "此账号已有登录状态，请先退出登录再重新扫码",
                    }.get(error.code)
                    or {
                        0: "微信引擎暂时没有响应，请稍后重试",
                        403: "微信引擎权限校验失败，请重启微信消息适配器",
                        404: "账号或登录操作不存在，请刷新页面",
                        409: "微信引擎当前无法执行此操作，请稍后重试并检查插件日志",
                        429: "操作过于频繁，请稍后重试",
                    }.get(error.status, "微信引擎处理失败，请稍后重试"),
                    "code": error.code,
                }
            )

    return invoke


async def body():
    value = await request.json()
    if not isinstance(value, dict):
        raise ValueError("请求格式无效")
    return value


def account_id(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("请先选择微信账号")
    return value


def bound_account(binding, accounts):
    if binding.get("account_id"):
        return binding["account_id"]
    authenticated = [a["id"] for a in accounts if a.get("authenticated") is True]
    return authenticated[0] if len(authenticated) == 1 else None


class NativeView:
    def __init__(self, context, runtime):
        self.context, self.runtime = context, runtime
        self._platform_lock = asyncio.Lock()

    def bindings(self):
        return [
            p
            for p in self.context.get_config().get("platform", [])
            if isinstance(p, dict) and p.get("type") == ADAPTER_ID and not p.get("server_url")
        ]

    @guarded
    async def status(self):
        if not self.runtime.ready:
            return {
                "engine_ready": False,
                "reason": self.runtime.reason or "微信引擎尚未启动",
                "accounts": [],
            }
        accounts = await self.runtime.accounts()
        bindings = self.bindings()
        labels = self.runtime.account_labels()
        return {
            "engine_ready": True,
            "engine_version": self.runtime.version,
            "accounts": [
                {
                    "id": a["id"],
                    "alias": labels.get(a["id"])
                    or a.get("alias")
                    or a.get("ownUsername")
                    or a["id"],
                    "authenticated": a.get("authenticated") is True,
                    "connection": a.get("connection", "disconnected"),
                    "platform_id": next(
                        (b["id"] for b in bindings if bound_account(b, accounts) == a["id"]), None
                    ),
                    **(
                        {"login_operation_id": attempt["id"]}
                        if (attempt := self.runtime.login_attempts.get(a["id"], {})).get("id")
                        else {}
                    ),
                }
                for a in accounts
            ],
        }

    @guarded
    async def start(self):
        await self.runtime.start()
        return {"engine_ready": True}

    @guarded
    async def create_account(self):
        data = await body()
        alias = data.get("alias", "微信")
        if (
            not isinstance(alias, str)
            or not alias.strip()
            or len(alias) > 64
            or any(ord(c) < 32 for c in alias)
        ):
            raise ValueError("账号名称须为 1～64 个字符")
        try:
            request_id = UUID(data.get("request_id", ""))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("创建请求标识无效，请重新打开添加账号窗口") from None
        target = "webridge-" + request_id.hex
        async with self.runtime.account_lock(target):
            accounts = await self.runtime.accounts()
            existing = next((a for a in accounts if a["id"] == target), None)
            if existing is None:
                await self.runtime.client._request("POST", "/accounts", json={"id": target})
            await self.runtime.set_account_label(target, alias.strip())
        return {"account_id": target}

    async def _operation(self, client, account, operation_id):
        op = await client.operation(operation_id)
        if (
            op.get("id") != operation_id
            or op.get("accountId") != account
            or op.get("kind") != "login"
        ):
            raise RuntimeUnavailable("登录操作身份校验失败，请刷新账号状态")
        result = op.get("result")
        result = result if isinstance(result, dict) else {}
        state = op.get("state")
        if not isinstance(state, str) or not state:
            raise RuntimeUnavailable("登录操作状态无效，请重启插件后重试")
        terminal = state in TERMINAL_LOGIN_STATES
        public = {
            "operation_id": operation_id,
            "state": state,
            "terminal": terminal,
            "qr_generation": result.get("qrGeneration"),
            "qr_available": False,
        }
        generation = result.get("qrGeneration")
        if (
            not terminal
            and result.get("qrAvailable") is True
            and type(generation) is int
            and generation > 0
        ):
            try:
                png = await client.login_qr(operation_id, generation)
                public["qr"] = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
                public["qr_available"] = True
            except NativeAPIError as error:
                if error.status not in {404, 409}:
                    raise
        if terminal:
            attempt = self.runtime.login_attempts.get(account)
            if attempt and attempt.get("id") == operation_id:
                self.runtime.login_attempts.pop(account, None)
            if state != "succeeded":
                code = result.get("code")
                safe_code = (
                    code if isinstance(code, str) and code in LOGIN_FAILURE_REASONS else state
                )
                logger.info(
                    "微信登录结束：operation=%s state=%s code=%s", operation_id, state, safe_code
                )
                public["reason"] = LOGIN_FAILURE_REASONS.get(
                    code if isinstance(code, str) else ""
                ) or {
                    "cancelled": "登录已取消，可以重新获取二维码",
                    "expired": "登录二维码已过期，请重新获取二维码",
                    "rejected": "手机端已拒绝本次登录，请重新获取二维码",
                }.get(state, "微信登录未完成，请检查服务器网络或插件日志后重试")
        return public

    async def _recover_login(self, client, account):
        # Use the existing read-only event log to recover a login whose ID was
        # lost by an older UI. Never cancel or create an additional operation.
        try:
            page = await client._request(
                "GET", client.prefix + "/event-log", params={"after": "0", "limit": "200"}
            )
        except NativeAPIError as error:
            if error.status in {404, 410}:
                return None
            raise
        items = page.get("items")
        if not isinstance(items, list) or len(items) > 200:
            return None
        seen = set()
        for item in reversed(items):
            if not isinstance(item, dict) or item.get("kind") != "operation.updated":
                continue
            data = item.get("data")
            if not isinstance(data, dict) or data.get("kind") != "login":
                continue
            operation_id = data.get("id")
            if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 512:
                continue
            if operation_id in seen:
                continue
            seen.add(operation_id)
            if len(seen) > 8:
                break
            if data.get("state") in TERMINAL_LOGIN_STATES:
                continue
            try:
                current = await self._operation(client, account, operation_id)
            except NativeAPIError as error:
                if error.status == 404:
                    continue
                raise
            if not current["terminal"]:
                return current
        return None

    @guarded
    async def login(self):
        data = await body()
        account = account_id(data.get("account_id"))
        async with self.runtime.account_lock(account):
            async with await self.runtime.account_client(account) as client:
                attempt = self.runtime.login_attempts.get(account)
                if attempt and attempt.get("id"):
                    try:
                        current = await self._operation(client, account, attempt["id"])
                    except NativeAPIError as error:
                        if error.status != 404:
                            raise
                    else:
                        if not current["terminal"]:
                            return current
                    attempt = None
                if attempt is None:
                    attempt = {"key": "webridge-login-" + uuid4().hex}
                    self.runtime.login_attempts[account] = attempt
                try:
                    op = await client.login(attempt["key"])
                except NativeAPIError as error:
                    if error.code == "operation_in_progress":
                        current = await self._recover_login(client, account)
                        if current is not None:
                            attempt["id"] = current["operation_id"]
                            return current
                    raise
                if not isinstance(op.get("id"), str) or not op["id"]:
                    raise RuntimeUnavailable("微信引擎未返回登录操作标识")
                attempt["id"] = op["id"]
                return await self._operation(client, account, op["id"])

    @guarded
    async def login_status(self):
        account = account_id(request.query.get("account_id"))
        operation_id = request.query.get("operation_id", "")
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 512:
            raise ValueError("登录操作标识无效")
        async with await self.runtime.account_client(account) as client:
            return await self._operation(client, account, operation_id)

    @guarded
    async def cancel_login(self):
        data = await body()
        account = account_id(data.get("account_id"))
        async with self.runtime.account_lock(account):
            attempt = self.runtime.login_attempts.get(account)
            if not attempt or not attempt.get("id"):
                return {"state": "cancelled"}
            async with await self.runtime.account_client(account) as client:
                await client.cancel_operation(attempt["id"])
            self.runtime.login_attempts.pop(account, None)
            return {"state": "cancelled"}

    @guarded
    async def logout(self):
        data = await body()
        account = account_id(data.get("account_id"))
        if data.get("confirm") != account:
            raise ValueError("请确认要退出的微信账号")
        try:
            request_id = UUID(data.get("request_id", ""))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("退出请求标识无效") from None
        async with self.runtime.account_lock(account):
            attempt = self.runtime.login_attempts.get(account)
            async with await self.runtime.account_client(account) as client:
                if attempt and attempt.get("id"):
                    operation = await self._operation(client, account, attempt["id"])
                    if not operation["terminal"]:
                        raise ValueError("请先取消当前登录操作，再退出账号")
                result = await client.logout("webridge-logout-" + request_id.hex)
            return {"state": result.get("state", "queued")}

    @guarded
    async def platform(self):
        data = await body()
        account = account_id(data.get("account_id"))
        accounts = await self.runtime.accounts()
        chosen = next((a for a in accounts if a["id"] == account), None)
        if chosen is None or chosen.get("authenticated") is not True:
            raise ValueError("请先完成微信扫码登录")
        async with self._platform_lock:
            config = self.context.get_config()
            platforms = config.setdefault("platform", [])
            matches = [p for p in self.bindings() if bound_account(p, accounts) == account]
            if len(matches) > 1:
                raise ValueError("此账号有多个消息平台，请先在机器人页面保留一个")
            existing = matches[0] if matches else None
            platform_id = (
                existing["id"]
                if existing
                else "webridge-" + hashlib.sha256(account.encode()).hexdigest()[:12]
            )
            if existing is None and any(p.get("id") == platform_id for p in platforms):
                raise ValueError("机器人标识已被占用，请先在机器人页面调整")
            previous = dict(existing) if existing is not None else None
            target = (
                existing
                if existing is not None
                else {**DEFAULT_CONFIG, "id": platform_id, "account_id": account}
            )
            target["enable"] = True
            target["account_id"] = account
            if existing is None:
                platforms.append(target)
            try:
                config.save_config()
                await self.context.platform_manager.reload(target)
                instance = self.context.get_platform_inst(platform_id)
                if instance is None or instance.meta().name != ADAPTER_ID:
                    raise RuntimeError("PlatformManager did not create the adapter")
            except Exception:
                if existing is None:
                    platforms.remove(target)
                else:
                    existing.clear()
                    existing.update(previous)
                config.save_config()
                raise RuntimeUnavailable(
                    "消息平台启动失败，已恢复原配置；请检查 AstrBot 日志"
                ) from None
            return {"platform_id": platform_id}
