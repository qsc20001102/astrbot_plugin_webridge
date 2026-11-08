"""Exercise the transport against AstrBot 4.29.4's actual policy stages."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, AtAll, File, Image, Plain, Record
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.event_bus import UNIQUE_SESSION_ID_BUILDERS, EventBus
from astrbot.core.pipeline.context import PipelineContext
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.pipeline.whitelist_check.stage import WhitelistCheckStage
from conftest import module
from test_adapter import make_adapter, message, native_event, no_telemetry  # noqa: F401
from test_connection_view import plugin
from test_voice import drain_voice, env


def profile(**settings):
    config = deepcopy(DEFAULT_CONFIG)
    config["platform_settings"].update(settings)
    config["platform_settings"]["segmented_reply"]["enable"] = False
    config["wake_prefix"] = ["/"]
    config["t2i"] = False
    config["provider_tts_settings"]["enable"] = False
    return config


async def no_tts(_):
    return None


def pipeline_context(config):
    return PipelineContext(
        astrbot_config=config,
        plugin_manager=SimpleNamespace(
            context=SimpleNamespace(get_using_tts_provider_async=no_tts)
        ),
        astrbot_config_id="test-profile",
        db_helper=None,
    )


@pytest.fixture(autouse=True)
def isolated_session_preferences(monkeypatch):
    # The actual policy stages run unchanged; no account/database is needed.
    from astrbot.core import sp

    async def empty_preferences(**_):
        return {}

    monkeypatch.setattr(sp, "get_async", empty_preferences)


@pytest.mark.parametrize("thread", ["peer", "room@chatroom"])
@pytest.mark.parametrize("age", [-86400, 86400])
async def test_live_backlog_and_legacy_rules_do_not_preempt_profiles(serve, tmp_path, thread, age):
    msg = message(
        threadId=thread, createdAt=(datetime.now(UTC) - timedelta(seconds=age)).isoformat()
    )
    adapter, queue, _, sent = await make_adapter(
        serve,
        tmp_path,
        msg,
        enable_private_reply=False,
        allowed_users=["someone-else"],
        allowed_groups=["another-group"],
        group_reply_mode="disabled",
        group_reply_style="quote",
        unique_session=True,
        enable_media=False,
    )
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        assert event.message_str == msg["body"]
        assert not any(isinstance(part, At) for part in event.get_messages())
        await event.send(MessageChain().message("plain reply"))
        assert sent[0][1] == {"threadId": thread, "text": "plain reply"}
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "updates,origin",
    [
        ({"direction": "outgoing"}, "live"),
        ({"senderId": "bot"}, "live"),
        ({"recall": {"observedAt": "now"}}, "live"),
        ({"type": 47}, "live"),
        ({"senderId": ""}, "live"),
        ({}, "history"),
        ({}, "unknown"),
    ],
)
async def test_transport_still_rejects_echoes_imports_and_unsupported_payloads(
    serve, tmp_path, updates, origin
):
    adapter, queue, msg, sent = await make_adapter(serve, tmp_path, message(**updates))
    try:
        await adapter.process_native_event(native_event(msg, origin=origin))
        assert queue.empty()
        assert sent == []
        assert adapter.state.cursor == "1"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "body,mentions,wakes",
    [
        ("/help", {"state": "known", "self": False}, True),
        ("ordinary text", {"state": "known", "self": False}, False),
        ("@bot hello", {"state": "known", "self": False}, False),
        ("hello", {"state": "known", "self": True}, True),
        ("hello", {"state": "unknown", "self": True}, False),
        ("hello", {"state": "known", "userIds": ["other"]}, False),
    ],
)
async def test_actual_group_waking_stage_decides(serve, tmp_path, body, mentions, wakes):
    msg = message(threadId="room@chatroom", body=body, mentions=mentions)
    adapter, queue, _, _ = await make_adapter(serve, tmp_path, msg, group_reply_mode="all")
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        config = profile()
        config["plugin_set"] = ["chosen-plugin"]
        stage = WakingCheckStage()
        await stage.initialize(pipeline_context(config))
        await stage.process(event)
        assert event.is_wake is wakes
        assert event.is_stopped() is not wakes
        assert event.plugins_name == ["chosen-plugin"]
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "needs_prefix,body,wakes",
    [
        (True, "hello", False),
        (True, "/hello", True),
        (False, "hello", True),
    ],
)
async def test_private_prefix_follows_astrbot(serve, tmp_path, needs_prefix, body, wakes):
    adapter, queue, msg, _ = await make_adapter(serve, tmp_path, message(body=body))
    try:
        await adapter.process_native_event(native_event(msg))
        stage = WakingCheckStage()
        await stage.initialize(
            pipeline_context(profile(friend_message_needs_wake_prefix=needs_prefix))
        )
        event = queue.get_nowait()
        await stage.process(event)
        assert event.is_wake is wakes
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("ignore_all", [False, True])
async def test_genuine_at_all_is_controlled_by_astrbot(serve, tmp_path, ignore_all):
    msg = message(threadId="room@chatroom", mentions={"state": "known", "all": True})
    adapter, queue, _, _ = await make_adapter(serve, tmp_path, msg)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        assert any(isinstance(part, AtAll) for part in event.get_messages())
        stage = WakingCheckStage()
        await stage.initialize(pipeline_context(profile(ignore_at_all=ignore_all)))
        await stage.process(event)
        assert event.is_wake is not ignore_all
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("allowed", [False, True])
async def test_actual_whitelist_stage_decides(serve, tmp_path, allowed):
    adapter, queue, msg, _ = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        config = profile(
            enable_id_white_list=True,
            id_whitelist=[event.unified_msg_origin if allowed else "someone-else"],
        )
        stage = WhitelistCheckStage()
        await stage.initialize(pipeline_context(config))
        await stage.process(event)
        assert event.is_stopped() is not allowed
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "mention,quote", [(False, False), (True, False), (False, True), (True, True)]
)
async def test_actual_reply_decorator_controls_native_payload(serve, tmp_path, mention, quote):
    adapter, queue, msg, sent = await make_adapter(
        serve, tmp_path, message(threadId="room@chatroom")
    )
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        event.set_result(event.plain_result("answer"))
        stage = ResultDecorateStage()
        await stage.initialize(
            pipeline_context(profile(reply_with_mention=mention, reply_with_quote=quote))
        )
        async for _ in stage.process(event):
            pass
        await event.send(event.get_result())
        payload = sent[0][1]
        assert payload.get("mentions") == ({"userIds": ["user"]} if mention else None)
        assert payload.get("quote") == ({"messageId": "m1", "serverId": "900"} if quote else None)
        assert payload["text"].strip() == "answer"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("isolated", [False, True])
async def test_event_bus_uses_selected_profile_before_isolation(serve, tmp_path, isolated):
    instance, _ = plugin([])
    await instance.initialize()
    adapter, queue, msg, _ = await make_adapter(
        serve,
        tmp_path,
        message(threadId="room@chatroom"),
        framework_settings={"unique_session": not isolated},
    )
    processed = asyncio.Event()
    origins, handled = [], []

    def select_config(origin):
        origins.append(origin)
        return {"id": "selected", "name": "selected"}

    async def execute(event):
        handled.append(event)
        processed.set()

    scheduler = SimpleNamespace(
        ctx=SimpleNamespace(astrbot_config=profile(unique_session=isolated)), execute=execute
    )
    bus = EventBus(queue, {"selected": scheduler}, SimpleNamespace(get_conf_info=select_config))
    task = asyncio.create_task(bus.dispatch())
    try:
        await adapter.process_native_event(native_event(msg))
        await asyncio.wait_for(processed.wait(), 2)
        event = handled[0]
        assert origins == ["native-a:GroupMessage:a|group|room%40chatroom"]
        assert event.get_session_id() == "a|group|room%40chatroom" + ("|user" if isolated else "")
        assert event.get_extra("_session_isolated") is isolated
        await adapter.send_by_session(event.session, MessageChain().message("proactive"))
        processed.clear()
        msg.update(id="second", senderId="other", serverId="901")
        await adapter.process_native_event(native_event(msg, "2"))
        await asyncio.wait_for(processed.wait(), 2)
        assert origins[0] == origins[1]
        assert (handled[0].get_session_id() != handled[1].get_session_id()) is isolated
    finally:
        task.cancel()
        await asyncio.gather(task, *list(bus._pending_tasks), return_exceptions=True)
        await adapter.terminate()
        await instance.terminate()


async def test_session_registration_cleans_up_only_its_own_builder(monkeypatch):
    instance, _ = plugin([])
    monkeypatch.delitem(UNIQUE_SESSION_ID_BUILDERS, "webridge", raising=False)
    await instance.initialize()
    assert UNIQUE_SESSION_ID_BUILDERS["webridge"] is module("main").webridge_session_builder
    await instance.terminate()
    assert "webridge" not in UNIQUE_SESSION_ID_BUILDERS

    def foreign(event):
        return "foreign"

    monkeypatch.setitem(UNIQUE_SESSION_ID_BUILDERS, "webridge", foreign)
    with pytest.raises(RuntimeError, match="其他组件"):
        await instance.initialize()
    await instance.terminate()
    assert UNIQUE_SESSION_ID_BUILDERS["webridge"] is foreign


async def test_no_adapter_content_circuit_breaker(serve, tmp_path):
    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        for i in range(12):
            msg.update(id=f"m{i}", serverId=str(1000 + i))
            await adapter.process_native_event(native_event(msg, str(i + 1)))
            await queue.get_nowait().send(MessageChain().message("same answer"))
        assert len(sent) == 12
    finally:
        await adapter.terminate()


async def test_group_media_is_delivered_without_mention_or_prompts(serve, tmp_path):
    adapter, queue, msg, sent = await make_adapter(
        serve, tmp_path, message(type=3, body="", threadId="room@chatroom")
    )
    try:
        await adapter.process_native_event(native_event(msg))
        event = await asyncio.wait_for(queue.get(), 2)
        assert [type(part) for part in event.get_messages()] == [Image]
        assert event.message_str == ""
        assert sent == []
    finally:
        await adapter.terminate()


async def test_group_voice_and_explicit_quote_need_no_mention(serve, tmp_path):
    voice = message(id="original", serverId="800", type=34, body=None, threadId="room@chatroom")
    request = message(
        id="quote",
        threadId="room@chatroom",
        type=49,
        appMessageType=57,
        body="听听",
        quote={"serverId": "800", "senderId": "user", "type": 34, "state": "unsupported"},
    )
    result = await env(serve, tmp_path, request)
    result.add_voice(voice)
    try:
        await drain_voice(result, voice)
        assert any(isinstance(part, Record) for part in result.queue.get_nowait().get_messages())
        await drain_voice(result, request, "2")
        event = result.queue.get_nowait()
        assert event.message_str == "听听"
        assert any(isinstance(part, Record) for part in event.get_messages())
        assert result.posts == []
    finally:
        await result.adapter.terminate()


@pytest.mark.parametrize("outcome", ["accepted", "recalled", "failed", "auth_failed"])
async def test_explicit_group_mention_refreshes_before_send_intent(serve, tmp_path, outcome):
    from aiohttp import web

    requests = []
    refreshed = False
    msg = message(threadId="room@chatroom")

    async def custom_serve(fallback):
        async def handler(req):
            nonlocal refreshed
            requests.append((req.method, req.path))
            if req.path.endswith("/members/refresh"):
                return web.json_response(
                    {"id": "op1", "accountId": "a", "kind": "metadata", "state": "queued"},
                    status=202,
                )
            if "/operations/" in req.path:
                refreshed = True
                if outcome == "recalled":
                    msg["recall"] = {"observedAt": "now"}
                return web.json_response(
                    {
                        "id": "op1",
                        "accountId": "a",
                        "kind": "metadata",
                        "state": "succeeded" if outcome in {"accepted", "recalled"} else "failed",
                        "result": {
                            "code": "auth_required"
                            if outcome == "auth_failed"
                            else "operation_failed"
                        },
                    }
                )
            if "/members/" in req.path and not refreshed:
                return web.json_response({"error": {"code": "media_not_cached"}}, status=404)
            return await fallback(req)

        return await serve(handler)

    adapter, queue, _, sent = await make_adapter(custom_serve, tmp_path, msg)
    adapter.client.poll_interval = 0.001
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        chain = MessageChain([At(qq="user"), Plain("answer")])
        if outcome in {"failed", "auth_failed"}:
            with pytest.raises(ValueError, match="群成员"):
                await event.send(chain)
        else:
            await event.send(chain)
        assert len(sent) == (1 if outcome == "accepted" else 0)
        assert adapter.state.db.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == len(sent)
        assert requests.count(("POST", "/v1/accounts/a/groups/room@chatroom/members/refresh")) == 1
        if sent:
            assert sent[0][1]["mentions"] == {"userIds": ["user"]}
        if outcome == "recalled":
            assert event.is_stopped()
            assert adapter.state.message_status(msg["id"]) == "recalled"
    finally:
        await adapter.terminate()


async def test_keywords_are_ordinary_text_and_do_not_attach_recent_media(serve, tmp_path):
    msg = message(
        threadId="room@chatroom",
        body="@bot\u2005分析刚才的图片",
        mentions={"state": "known", "self": True},
    )
    adapter, queue, _, sent = await make_adapter(serve, tmp_path, msg)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        assert event.message_str == "分析刚才的图片"
        assert not any(isinstance(part, (Image, Record, File)) for part in event.get_messages())
        assert sent == []
    finally:
        await adapter.terminate()


def test_plugin_registers_no_commands_tools_or_llm_hooks():
    from astrbot.core.star.star_handler import star_handlers_registry

    main = module("main")
    assert not any(
        handler.handler_module_path == main.__name__ for handler in star_handlers_registry
    )
