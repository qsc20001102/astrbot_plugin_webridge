"""Read-only Plugin Views API, using real AstrBot request/response contracts."""

import json

import pytest
from aiohttp import web
from conftest import module


def plugin(robots):
    from astrbot.api.star import Context

    context = Context.__new__(Context)
    context._config = {"platform": robots}
    context.registered_web_apis = []
    return module("main").WeBridgePlugin(context), context


def saved_robot(url, **updates):
    return {
        "id": "native-1",
        "type": "webridge",
        "enable": False,
        "server_url": url,
        "account_id": "a",
        "token": "synthetic-view-token",
        **updates,
    }


async def api(context, endpoint, query=""):
    from astrbot.api.web import PluginRequest, bind_request_context
    from starlette.requests import Request

    path = "/astrbot_plugin_webridge/connection/" + endpoint
    matches = [
        (handler, methods)
        for route, handler, methods, _ in context.registered_web_apis
        if route == path
    ]
    assert len(matches) == 1, f"missing registered connection API: {endpoint}"
    handler, methods = matches[0]
    assert methods == ["GET"]
    req = Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": query.encode(),
            "headers": [],
        }
    )
    with bind_request_context(PluginRequest(req)):
        response = await handler()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return json.loads(response.body)


async def test_lists_saved_bots_including_disabled_without_exposing_credentials():
    _, context = plugin(
        [
            saved_robot("https://secret-host.example"),
            {"type": "telegram", "id": "other", "token": "other-token"},
            saved_robot("http://other", id="native-2", token_file="/private/token"),
        ]
    )
    assert await api(context, "bots") == {"bots": [{"id": "native-1"}, {"id": "native-2"}]}


async def test_connected_uses_saved_settings_and_only_two_gets(serve, tmp_path):
    calls = []

    async def handler(req):
        calls.append((req.method, req.raw_path))
        assert req.headers["Authorization"] == "Bearer synthetic-file-token"
        if req.path == "/prefix/v1/status":
            return web.json_response({"state": "ready", "serverId": "s1"})
        return web.json_response(
            {
                "id": "account/a",
                "authenticated": True,
                "connection": "online",
                "ownUsername": "private-username",
            }
        )

    url = await serve(handler)
    token_file = tmp_path / "token"
    token_file.write_text("synthetic-file-token\n")
    robot = saved_robot(url + "/prefix", account_id="account/a", token_file=str(token_file))
    _, context = plugin([robot])
    before = dict(robot)
    result = await api(context, "check", "bot_id=native-1&server_url=http://untrusted.invalid")
    assert result == {"state": "connected", "wechat_online": True, "reason": ""}
    assert calls == [("GET", "/prefix/v1/status"), ("GET", "/prefix/v1/accounts/account%2Fa")]
    assert robot == before
    assert not list(tmp_path.glob("*.sqlite3"))
    assert "token" not in json.dumps(result)
    assert "private-username" not in json.dumps(result)


@pytest.mark.parametrize(
    "status,account,want_online,reason",
    [
        (
            {"state": "ready"},
            {"id": "a", "authenticated": False, "connection": "disconnected"},
            False,
            "微信账号尚未登录",
        ),
        (
            {"state": "ready"},
            {"id": "a", "authenticated": True, "connection": "reconnecting"},
            False,
            "微信账号当前不在线",
        ),
        (
            {"state": "ready"},
            {"id": "foreign", "authenticated": True, "connection": "online"},
            None,
            "Account ID 与 Native 返回的账号不一致",
        ),
        ({"state": "starting"}, {}, None, "Native 服务尚未就绪"),
        ({}, {}, None, "Native 服务响应无效"),
        ({"state": "ready"}, {}, None, "Native 账号响应无效"),
    ],
)
async def test_short_failure_reasons(serve, status, account, want_online, reason):
    async def handler(req):
        return web.json_response(status if req.path.endswith("/status") else account)

    url = await serve(handler)
    _, context = plugin([saved_robot(url)])
    assert await api(context, "check", "bot_id=native-1") == {
        "state": "connected" if want_online is False else "failed",
        "wechat_online": want_online,
        "reason": reason,
    }


@pytest.mark.parametrize(
    "status,reason",
    [
        (401, "Native Token 无效或已过期"),
        (403, "Native Token 无权读取服务或该账号"),
        (404, "Native API 地址或 Account ID 不存在"),
        (503, "Native 服务暂时不可用"),
    ],
)
async def test_http_failure_never_exposes_error_body(serve, status, reason):
    paths = []

    async def handler(req):
        paths.append(req.path)
        return web.Response(status=status, text="synthetic-view-token /secret/path PRIVATE CHAT")

    url = await serve(handler)
    _, context = plugin([saved_robot(url)])
    assert await api(context, "check", "bot_id=native-1") == {
        "state": "failed",
        "wechat_online": None,
        "reason": reason,
    }
    assert paths == ["/v1/status"]


async def test_missing_or_wrong_robot_never_requests_native():
    _, context = plugin([{"id": "other", "type": "telegram", "server_url": "http://invalid"}])
    for query in ("", "bot_id=other", "bot_id=unknown"):
        result = await api(context, "check", query)
        assert result["state"] == "failed"
        assert result["reason"] == "请选择已保存的 微信消息适配器 机器人"


async def test_invalid_or_unreadable_configuration_is_safe():
    for robot in (
        saved_robot("file:///secret-token"),
        saved_robot("http://unreachable.invalid", token=""),
        saved_robot("http://unreachable.invalid", token_file="/missing/synthetic-token"),
    ):
        _, context = plugin([robot])
        result = await api(context, "check", "bot_id=native-1")
        assert result == {
            "state": "failed",
            "wechat_online": None,
            "reason": "连接配置无效，请检查地址、Account ID 和 Token 配置",
        }


async def test_each_check_reads_latest_saved_configuration(serve):
    calls = []

    async def handler(req):
        calls.append(req.headers["Authorization"])
        return web.json_response(
            {"state": "ready"}
            if req.path.endswith("status")
            else {"id": "a", "authenticated": True, "connection": "online"}
        )

    url = await serve(handler)
    _, context = plugin([saved_robot(url)])
    await api(context, "check", "bot_id=native-1")
    context._config["platform"][0]["token"] = "new-synthetic-token"
    await api(context, "check", "bot_id=native-1")
    assert calls == ["Bearer synthetic-view-token"] * 2 + ["Bearer new-synthetic-token"] * 2


async def test_connection_view_is_discovered_by_astrbot():
    from types import SimpleNamespace

    from astrbot.core.star.star import StarMetadata
    from astrbot.dashboard.services.plugin_page_service import PluginPageService
    from conftest import ROOT

    service = PluginPageService(SimpleNamespace(plugin_store_path=str(ROOT.parent)))
    metadata = StarMetadata(name="astrbot_plugin_webridge", root_dir_name=ROOT.name)
    pages = await service.discover_plugin_pages(metadata)
    assert [page.name for page in pages] == ["accounts", "connection"]
    assert pages[0].entry_file == "index.html"
