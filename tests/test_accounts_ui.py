"""Chromium exercises the shipped account page through its actual bridge API shape."""

import base64
import json
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright
from test_native_client import VALID_PNG

VIEW = Path(__file__).resolve().parents[1] / "views/accounts"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shutil.which("chromium"))
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    page = browser.new_page(viewport={"width": 420, "height": 850})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def asset(route):
        name = Path(urlsplit(route.request.url).path).name or "index.html"
        file = VIEW / name
        if file.is_file():
            route.fulfill(
                path=file,
                content_type={
                    ".html": "text/html",
                    ".js": "text/javascript",
                    ".css": "text/css",
                    ".png": "image/png",
                }[file.suffix],
            )
        else:
            route.fulfill(status=404)

    page.route("http://plugin.test/**", asset)
    yield page
    assert not errors
    page.close()


def open_page(page, accounts=None, ready=True, login_state="awaiting_scan"):
    page.add_init_script(
        """
        window.calls = [];
        window.accounts = ACCOUNTS;
        window.engineReady = READY;
        window.loginState = LOGIN_STATE;
        window.loginReason = '';
        window.failedPolls = 0;
        window.failPlatform = false;
        window.AstrBotPluginView = {
          ready: async () => ({}),
          apiGet: async (endpoint, data) => {
            window.calls.push(['GET', endpoint, data || {}]);
            if (endpoint === 'native/status') return {
              ok:true, engine_ready:window.engineReady, engine_version:'1.5.1',
              reason:'测试引擎未启动', accounts:window.accounts
            };
            if (endpoint === 'native/login-status') {
              if (window.failedPolls > 0) {
                window.failedPolls--;
                return {ok:false,reason:'临时网络错误'};
              }
              const terminal = ['succeeded','failed','cancelled','expired','rejected',
                'needs_login','needs_review'].includes(window.loginState);
              const target = window.accounts.find(a => a.id === data.account_id);
              if (terminal && target) delete target.login_operation_id;
              const qr = window.loginState === 'awaiting_scan';
              return {ok:true, operation_id:data.operation_id, state:window.loginState,
                terminal, reason:window.loginReason, qr_available:qr, qr:qr ? QR_DATA : null};
            }
            throw new Error('unexpected GET endpoint ' + endpoint);
          },
          apiPost: async (endpoint, data) => {
            window.calls.push(['POST', endpoint, data || {}]);
            if (endpoint === 'native/start') { window.engineReady=true; return {ok:true}; }
            if (endpoint === 'native/accounts') {
              window.accounts.push({id:'one',alias:data.alias,
                authenticated:false,connection:'disconnected'});
              return {ok:true,account_id:'one'};
            }
            if (endpoint === 'native/login') {
              window.accounts.find(a => a.id === data.account_id).login_operation_id='login-one';
              return {ok:true,operation_id:'login-one',state:'starting',terminal:false};
            }
            if (endpoint === 'native/cancel-login') {
              delete window.accounts.find(a => a.id === data.account_id).login_operation_id;
              return {ok:true,state:'cancelled'};
            }
            if (endpoint === 'native/logout') {
              window.accounts[0].authenticated=false;
              return {ok:true,state:'queued'};
            }
            if (endpoint === 'native/platform') {
              if (window.failPlatform) return {ok:false,reason:'平台启动失败'};
              window.accounts[0].platform_id='webridge-one';
              return {ok:true,platform_id:'webridge-one'};
            }
            throw new Error('unexpected POST endpoint ' + endpoint);
          }
        };
    """.replace("ACCOUNTS", json.dumps(accounts or []))
        .replace("READY", json.dumps(ready))
        .replace("LOGIN_STATE", json.dumps(login_state))
        .replace(
            "QR_DATA", json.dumps("data:image/png;base64," + base64.b64encode(VALID_PNG).decode())
        )
    )
    page.goto("http://plugin.test/accounts/index.html")
    expect(page).to_have_title("微信消息适配器 · 微信管理")
    expect(page.get_by_role("heading", name="微信消息适配器", exact=True)).to_be_visible()
    expect(page.locator("#engine-status")).to_have_text("引擎已就绪" if ready else "引擎未就绪")


def one(logged_in=False):
    return {
        "id": "one",
        "alias": "我的微信",
        "authenticated": logged_in,
        "connection": "online" if logged_in else "disconnected",
    }


def test_empty_account_can_be_created_and_qr_displayed_without_connection_fields(page):
    open_page(page)
    expect(page.get_by_text("还没有微信账号", exact=True)).to_be_visible()
    expect(page.locator("#connect")).to_be_disabled()
    page.get_by_role("button", name="添加微信", exact=True).click()
    page.get_by_label("账号名称").fill("测试微信")
    page.get_by_role("button", name="添加并登录").click()
    expect(page.locator("#qr-image")).to_be_visible()
    expect(page.locator("#login-status")).to_have_text("等待手机微信扫码")
    posts = page.evaluate("window.calls.filter(c => c[0] === 'POST')")
    assert [p[1] for p in posts] == ["native/accounts", "native/login"]
    assert set(posts[0][2]) == {"alias", "request_id"}
    assert posts[1][2] == {"account_id": "one"}
    assert not page.get_by_text("Token", exact=True).count()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_cancel_login_and_account_switch_hide_old_qr(page):
    open_page(page, [one(), {**one(), "id": "two", "alias": "第二个微信"}])
    page.locator("#login").click()
    expect(page.locator("#qr-image")).to_be_visible()
    page.locator("#account-select").select_option("two")
    expect(page.locator("#qr-content")).to_be_hidden()
    page.locator("#login").click()
    page.get_by_role("button", name="取消登录", exact=True).click()
    expect(page.locator("#qr-content")).to_be_hidden()
    assert page.evaluate(
        "window.calls.some(c => c[1] === 'native/cancel-login' && c[2].account_id === 'two')"
    )


def test_logged_in_account_connects_to_astrbot_with_one_click(page):
    open_page(page, [one(True)])
    expect(page.get_by_role("heading", name="微信已登录", exact=True)).to_be_visible()
    page.get_by_role("button", name="一键接入 AstrBot", exact=True).click()
    expect(page.locator("#connect-result")).to_contain_text("webridge-one")
    expect(page.locator("#platform-state")).to_have_text("webridge-one")
    assert page.evaluate("window.calls.filter(c => c[1] === 'native/platform').length") == 1


def test_failed_platform_request_keeps_the_account_and_can_retry(page):
    open_page(page, [one(True)])
    page.evaluate("window.failPlatform = true")
    page.locator("#connect").click()
    expect(page.get_by_role("alert")).to_have_text("平台启动失败")
    expect(page.locator("#connect")).to_be_enabled()
    page.evaluate("window.failPlatform = false")
    page.locator("#connect").click()
    expect(page.locator("#connect-result")).to_contain_text("webridge-one")


def test_engine_failure_offers_start_and_logout_preserves_account(page):
    open_page(page, [one(True)], ready=False)
    expect(page.locator("#engine-help")).to_be_visible()
    expect(page.locator("#add-account")).to_be_disabled()
    page.get_by_role("button", name="重新启动", exact=True).click()
    expect(page.locator("#engine-status")).to_have_text("引擎已就绪")
    page.on("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="退出登录", exact=True).click()
    expect(page.locator("#account-state")).to_have_text("未登录")
    expect(page.locator("#account-select")).to_have_value("one")


def test_account_alias_is_rendered_as_text(page):
    open_page(page, [{**one(), "alias": '<img src=x onerror="window.injected=true">'}])
    assert page.locator("#account-select option").text_content().startswith("<img")
    assert page.evaluate("window.injected === undefined")


def test_starting_resuming_and_authenticating_keep_the_same_login_until_success(page):
    page.clock.install()
    open_page(page, [one()], login_state="starting")
    page.locator("#login").click()
    expect(page.locator("#login-status")).to_have_text("正在获取二维码，请稍候…")
    expect(page.locator("#cancel-login")).to_be_visible()
    expect(page.locator("#qr-image")).to_be_hidden()
    expect(page.locator("#error")).to_be_hidden()
    for state, text in [
        ("resuming", "正在恢复微信登录…"),
        ("awaiting_scan", "等待手机微信扫码"),
        ("awaiting_approval", "请在手机微信上确认登录"),
        ("authenticating", "手机已确认，正在完成登录…"),
    ]:
        page.evaluate("state => window.loginState=state", state)
        page.clock.fast_forward(2100)
        expect(page.locator("#login-status")).to_have_text(text)
        expect(page.locator("#cancel-login")).to_be_visible()
        expect(page.locator("#error")).to_be_hidden()
        if state == "awaiting_scan":
            expect(page.locator("#qr-image")).to_be_visible()
    page.evaluate("window.loginState='succeeded'; window.accounts[0].authenticated=true")
    page.clock.fast_forward(2100)
    expect(page.get_by_role("heading", name="微信已登录", exact=True)).to_be_visible()
    assert page.evaluate("window.calls.filter(c => c[1] === 'native/login').length") == 1


def test_page_refresh_resumes_pending_login_without_new_post(page):
    page.clock.install()
    account = {**one(), "login_operation_id": "existing-login"}
    open_page(page, [account], login_state="starting")
    expect(page.locator("#cancel-login")).to_be_visible()
    page.get_by_role("button", name="刷新", exact=True).click()
    expect(page.locator("#login-status")).to_have_text("正在获取二维码，请稍候…")
    page.reload()
    expect(page.locator("#cancel-login")).to_be_visible()
    expect(page.locator("#login-status")).to_have_text("正在获取二维码，请稍候…")
    page.evaluate("window.loginState='awaiting_scan'")
    page.clock.fast_forward(2100)
    expect(page.locator("#qr-image")).to_be_visible()
    assert page.evaluate("window.calls.filter(c => c[0] === 'POST').length") == 0
    assert page.evaluate(
        "window.calls.filter(c => c[1] === 'native/login-status')"
        ".every(c => c[2].operation_id === 'existing-login')"
    )


def test_expired_login_shows_reason_and_allows_user_to_get_a_new_qr(page):
    page.clock.install()
    open_page(page, [one()])
    page.locator("#login").click()
    expect(page.locator("#qr-image")).to_be_visible()
    page.evaluate(
        "window.loginState='expired'; window.loginReason='登录二维码已过期，请重新获取二维码'"
    )
    page.clock.fast_forward(2100)
    expect(page.locator("#error")).to_have_text("登录二维码已过期，请重新获取二维码")
    expect(page.locator("#login")).to_be_enabled()
    expect(page.locator("#qr-content")).to_be_hidden()
    page.evaluate("window.loginState='awaiting_scan'; window.loginReason=''")
    page.locator("#login").click()
    expect(page.locator("#qr-image")).to_be_visible()
    expect(page.locator("#error")).to_be_hidden()


def test_temporary_poll_error_recovers_without_leaving_a_stale_error_banner(page):
    page.clock.install()
    open_page(page, [one()], login_state="starting")
    page.evaluate("window.failedPolls=1")
    page.locator("#login").click()
    expect(page.locator("#error")).to_have_text("临时网络错误")
    expect(page.locator("#cancel-login")).to_be_visible()
    page.evaluate("window.loginState='awaiting_scan'")
    page.clock.fast_forward(4100)
    expect(page.locator("#qr-image")).to_be_visible()
    expect(page.locator("#error")).to_be_hidden()
    assert page.evaluate("window.calls.filter(c => c[1] === 'native/login').length") == 1
