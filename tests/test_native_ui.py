"""Real AstrBot request and Native HTTP clients; only external Native is simulated."""

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import unquote
from uuid import uuid4

import pytest
from aiohttp import web
from astrbot.api.web import PluginRequest, bind_request_context
from conftest import module
from starlette.requests import Request
from test_native_client import VALID_PNG


class SavedConfig(dict):
    saves = 0

    def save_config(self):
        self.saves += 1


async def invoke(view, method, data=None, query=""):
    payload = json.dumps(data or {}).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    req = Request(
        {
            "type": "http",
            "method": "POST" if data is not None else "GET",
            "path": "/plugin/test",
            "query_string": query.encode(),
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )
    with bind_request_context(PluginRequest(req)):
        result = await getattr(view, method)()
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    return json.loads(result.body)


@pytest.fixture
async def env(serve):
    native = module("native_client")
    accounts = [
        {
            "id": "one",
            "alias": "微信一",
            "authenticated": False,
            "connection": "disconnected",
            "credential": "do-not-expose",
        }
    ]
    operations, calls, login_keys = {}, [], []
    state = SimpleNamespace(
        login_fault=False,
        qr_fault=0,
        identity_fault=False,
        login_state="awaiting_scan",
        conflict=False,
    )

    async def handler(req):
        assert req.headers["Authorization"] == "Bearer internal-secret"
        calls.append((req.method, req.path))
        parts = req.path.split("/")
        if req.path.endswith("/accounts"):
            if req.method == "POST":
                data = await req.json()
                accounts.append(
                    {
                        "id": data["id"],
                        "alias": None,
                        "authenticated": False,
                        "connection": "disconnected",
                    }
                )
                return web.json_response(accounts[-1], status=201)
            return web.json_response({"items": accounts})
        account = unquote(parts[3])
        if req.path.endswith("/event-log"):
            assert req.query == {"after": "0", "limit": "200"}
            return web.json_response(
                {
                    "items": [
                        {
                            "cursor": str(i + 1),
                            "kind": "operation.updated",
                            "data": {"id": op["id"], "kind": op["kind"], "state": op["state"]},
                        }
                        for i, op in enumerate(operations.values())
                    ],
                    "nextCursor": str(len(operations)),
                }
            )
        if req.path.endswith("/auth/login"):
            data = await req.json()
            assert data["strategy"] == "qr" and data["forceQr"] is True
            assert data["allowQr"] is True and data["timeoutSeconds"] == 300
            assert data["qrRefresh"] == {"mode": "auto", "maxRefreshes": 3}
            key = req.headers["Idempotency-Key"]
            login_keys.append(key)
            if state.conflict:
                return web.json_response({"error": {"code": "operation_in_progress"}}, status=409)
            operations.setdefault(
                "login-" + account,
                {
                    "id": "login-" + account,
                    "accountId": account,
                    "kind": "login",
                    "state": state.login_state,
                    "result": {"qrGeneration": 1, "qrAvailable": True, "secret": "never-expose"},
                },
            )
            if state.login_fault:
                state.login_fault = False
                return web.json_response({}, status=503)
            return web.json_response(operations["login-" + account], status=202)
        if req.path.endswith("/qr"):
            assert req.query["generation"] == "1"
            if state.qr_fault:
                return web.json_response({}, status=state.qr_fault)
            return web.Response(body=VALID_PNG, content_type="image/png")
        if "/operations/" in req.path:
            if parts[-1] not in operations:
                return web.json_response({"error": {"code": "not_found"}}, status=404)
            op = dict(operations[parts[-1]])
            if req.method == "DELETE":
                op["state"] = operations[parts[-1]]["state"] = "cancelled"
            if state.identity_fault:
                op["accountId"] = "foreign"
            return web.json_response(op)
        if req.path.endswith("/logout"):
            assert req.headers["Idempotency-Key"].startswith("webridge-logout-")
            return web.json_response({"id": "logout-1", "state": "queued"}, status=202)
        return web.json_response({}, status=404)

    url = await serve(handler)

    class Runtime:
        ready = True
        reason = ""
        version = "1.5.1"

        def __init__(self):
            self.login_attempts, self.locks = {}, {}
            self.labels = {}
            self.client = native.NativeClient(url + "/v1", "", "internal-secret")

        def account_labels(self):
            return dict(self.labels)

        async def set_account_label(self, account, label):
            self.labels[account] = label

        def account_lock(self, account):
            return self.locks.setdefault(account, asyncio.Lock())

        async def start(self):
            await self.client.open()
            self.ready = True

        async def accounts(self):
            await self.start()
            return (await self.client.accounts())["items"]

        async def account_client(self, account):
            if not any(a["id"] == account for a in accounts):
                raise module("runtime").RuntimeUnavailable("账号不存在")
            client = native.NativeClient(url + "/v1", account, "internal-secret")
            await client.open()
            return client

    config = SavedConfig(platform=[])
    loaded = []

    async def reload_platform(platform):
        loaded.append(dict(platform))

    runtime = Runtime()
    context = SimpleNamespace(
        get_config=lambda: config,
        platform_manager=SimpleNamespace(reload=reload_platform),
        get_platform_inst=lambda platform_id: next(
            (
                SimpleNamespace(meta=lambda: SimpleNamespace(name="webridge"))
                for item in loaded
                if item["id"] == platform_id
            ),
            None,
        ),
    )
    view = module("native_ui").NativeView(context, runtime)
    yield SimpleNamespace(
        view=view,
        runtime=runtime,
        accounts=accounts,
        operations=operations,
        calls=calls,
        keys=login_keys,
        state=state,
        config=config,
        loaded=loaded,
    )
    await runtime.client.close()


async def test_status_returns_only_public_account_fields(env):
    result = await invoke(env.view, "status")
    assert result["engine_ready"] is True
    assert result["accounts"][0] == {
        "id": "one",
        "alias": "微信一",
        "authenticated": False,
        "connection": "disconnected",
        "platform_id": None,
    }
    assert "secret" not in json.dumps(result) and "credential" not in json.dumps(result)


async def test_create_account_is_idempotent_and_has_no_connection_parameters(env):
    data = {"alias": "新微信", "request_id": str(uuid4())}
    first = await invoke(env.view, "create_account", data)
    second = await invoke(env.view, "create_account", data)
    assert first == second and first["ok"] is True
    assert len(env.accounts) == 2
    assert env.calls.count(("POST", "/v1/accounts")) == 1
    assert env.runtime.labels[first["account_id"]] == "新微信"


async def test_login_returns_qr_and_repeated_clicks_reuse_one_attempt(env):
    first = await invoke(env.view, "login", {"account_id": "one"})
    second = await invoke(env.view, "login", {"account_id": "one"})
    assert first == second and first["ok"] is True
    assert first["qr"].startswith("data:image/png;base64,")
    assert len(env.keys) == 1
    assert "never-expose" not in json.dumps(first)


@pytest.mark.parametrize("phase", ["starting", "resuming", "authenticating", "future_login_step"])
async def test_native_nonterminal_login_is_retained_and_not_submitted_twice(env, phase):
    env.state.login_state = phase
    first = await invoke(env.view, "login", {"account_id": "one"})
    assert first["ok"] is True and first["state"] == phase
    assert env.runtime.login_attempts["one"]["id"] == "login-one"
    second = await invoke(env.view, "login", {"account_id": "one"})
    assert second == first and len(env.keys) == 1


async def test_refresh_can_resume_starting_login_without_exposing_request_key(env):
    env.state.login_state = "starting"
    await invoke(env.view, "login", {"account_id": "one"})
    status = await invoke(env.view, "status")
    assert status["accounts"][0]["login_operation_id"] == "login-one"
    assert env.keys[0] not in json.dumps(status)


async def test_conflict_recovers_existing_native_login_instead_of_starting_another(env):
    env.state.conflict = True
    env.operations["previous-login"] = {
        "id": "previous-login",
        "accountId": "one",
        "kind": "login",
        "state": "starting",
        "result": {},
    }
    result = await invoke(env.view, "login", {"account_id": "one"})
    assert result["ok"] is True and result["operation_id"] == "previous-login"
    assert env.runtime.login_attempts["one"]["id"] == "previous-login"
    assert len(env.keys) == 1


async def test_unrecoverable_conflict_reports_busy_without_cancelling_other_operations(env):
    env.state.conflict = True
    env.operations["metadata-one"] = {
        "id": "metadata-one",
        "accountId": "one",
        "kind": "metadata",
        "state": "running",
        "result": {},
    }
    result = await invoke(env.view, "login", {"account_id": "one"})
    assert result["ok"] is False and result["code"] == "operation_in_progress"
    assert "正在进行" in result["reason"] and "刷新" not in result["reason"]
    assert not any(method == "DELETE" for method, _ in env.calls)


async def test_conflict_recovery_validates_account_before_using_qr(env):
    env.state.conflict = True
    env.operations["foreign-login"] = {
        "id": "foreign-login",
        "accountId": "foreign",
        "kind": "login",
        "state": "awaiting_scan",
        "result": {"qrGeneration": 1, "qrAvailable": True},
    }
    result = await invoke(env.view, "login", {"account_id": "one"})
    assert result["ok"] is False and "身份" in result["reason"]
    assert not any(path.endswith("/qr") for _, path in env.calls)


@pytest.mark.parametrize(
    "state",
    ["cancelled", "succeeded", "failed", "expired", "rejected", "needs_login", "needs_review"],
)
async def test_only_native_terminal_states_end_the_attempt(env, state):
    env.state.login_state = state
    result = await invoke(env.view, "login", {"account_id": "one"})
    assert result["terminal"] is True and env.runtime.login_attempts == {}


async def test_starting_login_can_be_cancelled_before_qr_is_ready(env):
    env.state.login_state = "starting"
    await invoke(env.view, "login", {"account_id": "one"})
    assert (await invoke(env.view, "cancel_login", {"account_id": "one"}))["state"] == "cancelled"
    assert ("DELETE", "/v1/accounts/one/operations/login-one") in env.calls
    assert env.runtime.login_attempts == {}


@pytest.mark.parametrize("code", ["login_lan_challenge_failed", {"secret": "never-expose"}])
async def test_terminal_failure_returns_safe_actionable_reason(env, code):
    await invoke(env.view, "login", {"account_id": "one"})
    env.operations["login-one"].update(
        state="failed", result={"code": code, "hint": "never-expose"}
    )
    result = await invoke(env.view, "login_status", query="account_id=one&operation_id=login-one")
    assert result["terminal"] is True and result["reason"]
    assert "never-expose" not in json.dumps(result)
    if isinstance(code, str):
        assert "局域网" in result["reason"]


async def test_missing_cached_operation_can_start_a_new_login(env):
    env.runtime.login_attempts["one"] = {"id": "missing", "key": "old-key"}
    result = await invoke(env.view, "login", {"account_id": "one"})
    assert result["ok"] is True and result["operation_id"] == "login-one"
    assert env.keys[0] != "old-key"


async def test_lost_login_response_keeps_the_same_idempotency_key(env):
    env.state.login_fault = True
    assert (await invoke(env.view, "login", {"account_id": "one"}))["ok"] is False
    assert (await invoke(env.view, "login", {"account_id": "one"}))["ok"] is True
    assert len(env.keys) == 2 and env.keys[0] == env.keys[1]
    assert len(env.operations) == 1


@pytest.mark.parametrize("status", [404, 409])
async def test_qr_not_ready_or_replaced_does_not_end_the_login(env, status):
    env.state.qr_fault = status
    result = await invoke(env.view, "login", {"account_id": "one"})
    assert result["ok"] is True and result["qr_available"] is False
    assert result["state"] == "awaiting_scan"


async def test_operation_account_is_verified_before_fetching_qr(env):
    await invoke(env.view, "login", {"account_id": "one"})
    count = len(env.calls)
    env.state.identity_fault = True
    result = await invoke(env.view, "login_status", query="account_id=one&operation_id=login-one")
    assert result["ok"] is False
    assert not any(path.endswith("/qr") for _, path in env.calls[count:])


async def test_cancel_login_and_terminal_state_clear_owned_attempt(env):
    await invoke(env.view, "login", {"account_id": "one"})
    assert (await invoke(env.view, "cancel_login", {"account_id": "one"}))["state"] == "cancelled"
    assert env.runtime.login_attempts == {}
    assert ("DELETE", "/v1/accounts/one/operations/login-one") in env.calls
    env.operations["login-one"]["state"] = "awaiting_scan"
    await invoke(env.view, "login", {"account_id": "one"})
    env.operations["login-one"]["state"] = "succeeded"
    assert (await invoke(env.view, "login_status", query="account_id=one&operation_id=login-one"))[
        "state"
    ] == "succeeded"
    assert env.runtime.login_attempts == {}


async def test_platform_is_created_with_only_account_and_common_fields(env):
    assert (await invoke(env.view, "platform", {"account_id": "one"}))["ok"] is False
    env.accounts[0]["authenticated"] = True
    first = await invoke(env.view, "platform", {"account_id": "one"})
    second = await invoke(env.view, "platform", {"account_id": "one"})
    assert first == second and first["ok"] is True
    assert len(env.config["platform"]) == 1
    assert set(env.config["platform"][0]) == {"id", "type", "enable", "account_id"}
    assert env.config["platform"][0]["enable"] is True
    assert env.loaded[-1] == env.config["platform"][0]


async def test_failed_platform_load_restores_configuration(env):
    env.accounts[0]["authenticated"] = True

    async def fail(_):
        raise RuntimeError("synthetic load failure")

    env.view.context.platform_manager.reload = fail
    result = await invoke(env.view, "platform", {"account_id": "one"})
    assert result["ok"] is False and env.config["platform"] == []
    assert env.config.saves == 2


async def test_one_click_reuses_single_auto_selected_bot_instead_of_duplicating(env):
    env.accounts[0]["authenticated"] = True
    env.config["platform"].append(
        {"id": "manual", "type": "webridge", "enable": True, "account_id": ""}
    )
    assert (await invoke(env.view, "status"))["accounts"][0]["platform_id"] == "manual"
    result = await invoke(env.view, "platform", {"account_id": "one"})
    assert result == {"ok": True, "platform_id": "manual"}
    assert len(env.config["platform"]) == 1
    assert env.config["platform"][0]["account_id"] == "one"


async def test_platform_manager_silent_skip_is_not_reported_as_success(env):
    env.accounts[0]["authenticated"] = True
    env.view.context.get_platform_inst = lambda _: None
    result = await invoke(env.view, "platform", {"account_id": "one"})
    assert result["ok"] is False and env.config["platform"] == []


async def test_logout_checks_cached_login_state_before_requiring_cancellation(env):
    await invoke(env.view, "login", {"account_id": "one"})
    data = {"account_id": "one", "confirm": "one", "request_id": str(uuid4())}
    assert (await invoke(env.view, "logout", data))["ok"] is False
    assert not any(path.endswith("/logout") for _, path in env.calls)
    env.operations["login-one"]["state"] = "succeeded"
    assert (await invoke(env.view, "logout", data))["ok"] is True
    assert env.runtime.login_attempts == {}


async def test_logout_requires_exact_confirmation_and_retains_data(env):
    result = await invoke(
        env.view, "logout", {"account_id": "one", "confirm": "other", "request_id": str(uuid4())}
    )
    assert result["ok"] is False
    result = await invoke(
        env.view, "logout", {"account_id": "one", "confirm": "one", "request_id": str(uuid4())}
    )
    assert result["ok"] is True and result["state"] == "queued"
    assert not any(
        method == "DELETE" and path.endswith("/accounts/one") for method, path in env.calls
    )


async def test_managed_state_is_stable_when_engine_port_and_token_change(tmp_path, monkeypatch):
    adapter_module = module("adapter")
    config = module("config")
    current = config.NativeConfig("http://127.0.0.1:12345/v1", "one", "token-one", "")

    async def connection(account):
        assert account in {"", "one"}
        return current

    monkeypatch.setattr(
        adapter_module, "get_runtime", lambda: SimpleNamespace(connection=connection)
    )
    cfg = {**config.DEFAULT_CONFIG, "id": "managed-one"}
    adapter = adapter_module.WeBridgeAdapter(cfg, {}, asyncio.Queue(), state_dir=tmp_path)
    try:
        assert adapter.state is None
        await adapter._connect_managed()
        adapter.state.advance("7")
        path = next(tmp_path.glob("*.sqlite3"))
        current = config.NativeConfig("http://127.0.0.1:23456/v1", "one", "token-two", "")
        await adapter._connect_managed()
        assert adapter.state.cursor == "7" and len(list(tmp_path.glob("*.sqlite3"))) == 1
    finally:
        await adapter.terminate()
    restarted = adapter_module.WeBridgeAdapter(cfg, {}, asyncio.Queue(), state_dir=tmp_path)
    try:
        await restarted._connect_managed()
        assert restarted.state.cursor == "7" and path.exists()
    finally:
        await restarted.terminate()
