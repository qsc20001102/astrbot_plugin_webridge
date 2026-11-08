"""Run the shipped page in Chromium with only the AstrBot bridge replaced."""

import json
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

VIEW = Path(__file__).resolve().parents[1] / "views" / "connection"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=shutil.which("chromium"))
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    page = browser.new_page(viewport={"width": 420, "height": 640})

    def asset(route):
        name = Path(urlsplit(route.request.url).path).name or "index.html"
        path = VIEW / name
        if not path.is_file():
            route.fulfill(status=404, body="not found")
        else:
            mime = {
                ".html": "text/html",
                ".js": "text/javascript",
                ".css": "text/css",
                ".png": "image/png",
            }
            route.fulfill(path=path, content_type=mime[path.suffix])

    page.route("http://plugin.test/**", asset)
    yield page
    page.close()


def open_view(page, bots):
    assert (VIEW / "index.html").is_file(), "missing connection Plugin View"
    page.add_init_script(
        """
        window.calls = [];
        window.pendingChecks = [];
        window.AstrBotPluginView = {
            ready: () => new Promise(resolve => { window.releaseReady = resolve; }),
            apiGet: (endpoint, params) => {
                window.calls.push([endpoint, params || {}]);
                if (endpoint === 'connection/bots') return Promise.resolve({bots: BOTS});
                if (endpoint !== 'connection/check') throw new Error('unexpected endpoint');
                return new Promise((resolve, reject) => {
                    window.pendingChecks.push({resolve, reject});
                });
            }
        };
    """.replace("BOTS", json.dumps(bots))
    )
    page.goto("http://plugin.test/connection/index.html")
    page.wait_for_function("typeof window.releaseReady === 'function'")


def start_check(page):
    page.evaluate("window.releaseReady({})")
    page.wait_for_function("window.pendingChecks.length === 1")


def complete(page, result, index=0):
    page.evaluate(
        "([index, result]) => window.pendingChecks[index].resolve(result)", [index, result]
    )


def test_four_states_single_automatic_check_and_manual_retry(page):
    open_view(page, [{"id": "native-1"}])
    expect(page.get_by_role("heading", name="微信消息适配器")).to_be_visible()
    expect(page.locator("#connection-status")).to_have_text("未检测")
    start_check(page)
    expect(page.locator("#connection-status")).to_have_text("检测中")
    expect(page.get_by_role("button", name="测试连接")).to_be_disabled()
    complete(page, {"state": "connected", "wechat_online": True, "reason": ""})
    expect(page.locator("#connection-status")).to_have_text("已连接")
    expect(page.locator("#connection-status")).to_have_css("color", "rgb(21, 128, 61)")
    expect(page.locator("#wechat-status")).to_have_text("在线")
    expect(page.locator("#failure-reason")).to_be_hidden()
    expect(page.locator("#robot-picker")).to_be_hidden()
    page.wait_for_timeout(100)
    assert page.evaluate("window.calls") == [
        ["connection/bots", {}],
        ["connection/check", {"bot_id": "native-1"}],
    ]
    page.get_by_role("button", name="测试连接").click()
    page.wait_for_function("window.pendingChecks.length === 2")
    complete(
        page, {"state": "failed", "wechat_online": None, "reason": "Native Token 无效或已过期"}, 1
    )
    expect(page.locator("#connection-status")).to_have_text("连接失败")
    expect(page.locator("#connection-status")).to_have_css("color", "rgb(185, 28, 28)")
    expect(page.locator("#wechat-status")).to_have_text("未检测")
    expect(page.locator("#failure-reason")).to_have_text("Native Token 无效或已过期")
    expect(page.get_by_role("button", name="测试连接")).to_be_enabled()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_multiple_bots_selects_saved_id_and_tests_selected_bot(page):
    open_view(page, [{"id": "native-1"}, {"id": "native-2"}])
    start_check(page)
    expect(page.get_by_label("机器人")).to_be_disabled()
    complete(page, {"state": "connected", "wechat_online": True, "reason": ""})
    expect(page.get_by_label("机器人")).to_be_enabled()
    page.get_by_label("机器人").select_option("native-2")
    page.wait_for_function("window.pendingChecks.length === 2")
    assert page.evaluate("window.calls.at(-1)") == ["connection/check", {"bot_id": "native-2"}]
    complete(page, {"state": "connected", "wechat_online": True, "reason": ""}, 1)
    expect(page.locator("#connection-status")).to_have_text("已连接")


def test_no_configured_robot_has_useful_empty_state(page):
    open_view(page, [])
    page.evaluate("window.releaseReady({})")
    expect(page.locator("#connection-status")).to_have_text("连接失败")
    expect(page.locator("#failure-reason")).to_have_text(
        "请先在机器人配置中保存 微信消息适配器 机器人"
    )
    assert page.evaluate("window.pendingChecks.length") == 0


def test_bridge_failure_does_not_render_raw_exception(page):
    open_view(page, [{"id": "native-1"}])
    start_check(page)
    page.evaluate("window.pendingChecks[0].reject(new Error('SENSITIVE RAW ERROR'))")
    expect(page.locator("#failure-reason")).to_have_text(
        "检测请求失败，请确认 AstrBot 登录有效后重试"
    )
    expect(page.locator("body")).not_to_contain_text("SENSITIVE RAW ERROR")
    expect(page.get_by_role("button", name="测试连接")).to_be_enabled()


@pytest.mark.parametrize("reason", ["微信账号尚未登录", "微信账号当前不在线"])
def test_native_connected_and_wechat_offline_are_shown_separately(page, reason):
    open_view(page, [{"id": "native-1"}])
    start_check(page)
    complete(page, {"state": "connected", "wechat_online": False, "reason": reason})
    expect(page.locator("#connection-status")).to_have_text("已连接")
    expect(page.locator("#connection-status")).to_have_css("color", "rgb(21, 128, 61)")
    expect(page.locator("#wechat-status")).to_have_text("离线")
    expect(page.locator("#failure-reason")).to_be_visible()
    expect(page.locator("#failure-reason")).to_have_text(reason)
    expect(page.get_by_role("button", name="测试连接")).to_be_enabled()
