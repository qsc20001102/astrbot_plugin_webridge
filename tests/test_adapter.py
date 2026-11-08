import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiohttp import web
from conftest import module
from test_native_client import VALID_JPEG, VALID_PNG, media_asset, member_lookup


def message(**updates):
    data = {
        "id": "m1",
        "accountId": "a",
        "threadId": "peer",
        "serverId": "900",
        "senderId": "user",
        "direction": "incoming",
        "type": 1,
        "body": "你好",
        "createdAt": datetime.now(UTC).isoformat(),
        "version": "1",
        "media": [],
        "mentions": {"state": "known", "self": False, "all": False, "userIds": []},
    }
    data.update(updates)
    return data


def native_event(msg, cursor="1", origin="live"):
    return {
        "cursor": cursor,
        "kind": "message.committed",
        "data": {"messageId": msg["id"], "threadId": msg["threadId"], "origin": origin},
    }


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    from astrbot.core.utils.metrics import Metric

    async def upload(**kwargs):
        pass

    monkeypatch.setattr(Metric, "upload", upload)


async def make_adapter(
    serve, tmp_path, msg=None, *, media_data=VALID_JPEG, framework_settings=None, **options
):
    msg = msg or message()
    sent = []

    async def handler(req):
        if req.path.endswith("/media") and req.method == "GET":
            return web.json_response(
                {
                    "items": [
                        media_asset(
                            "asset-1",
                            accountId="a",
                            message_id=msg["id"],
                            data=media_data,
                        )
                    ]
                }
            )
        if req.path.endswith("/media/asset-1/content"):
            return web.Response(body=media_data, content_type="image/jpeg")
        if "/messages/" in req.path:
            return web.json_response(msg)
        if "/members/" in req.path and req.method == "GET":
            return web.json_response(member_lookup(req.path.rsplit("/", 1)[-1]))
        if req.method == "POST":
            sent.append((req.headers["Idempotency-Key"], await req.json()))
            return web.json_response(
                {
                    "id": "s1",
                    "state": "accepted",
                    "accountId": "a",
                    "destination": sent[-1][1]["threadId"],
                }
            )
        return web.json_response(
            {"id": "a", "ownUsername": "bot", "authenticated": True, "connection": "online"}
        )

    url = await serve(handler)
    cls = module("adapter").WeBridgeAdapter
    queue = asyncio.Queue()
    cfg = {"id": "native-a", "server_url": url, "account_id": "a", "token": "fake", **options}
    adapter = cls(cfg, framework_settings or {}, queue, state_dir=tmp_path)
    await adapter.client.open()
    adapter.bot_self_id = "bot"
    return adapter, queue, msg, sent


async def test_private_event_real_framework_lifecycle_and_reply(serve, tmp_path):
    from astrbot.api.event import AstrMessageEvent, MessageChain
    from astrbot.api.message_components import Plain
    from astrbot.api.platform import MessageType

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        assert isinstance(event, AstrMessageEvent)
        assert event.message_obj.type == MessageType.FRIEND_MESSAGE
        assert event.message_str == "你好"
        assert event.get_sender_id() == "user"
        assert event.platform_meta.id == "native-a"
        assert event.message_obj.self_id == "bot"
        assert event.session.session_id == "a|private|peer"
        await event.send(MessageChain([Plain("回复")]))
        assert event._has_send_oper
        assert sent[0][1] == {"threadId": "peer", "text": "回复"}
        assert adapter.state.message_status("m1") == "replied"
        assert adapter.state.send_record(sent[0][0])["state"] == "accepted"
        await adapter.process_native_event(native_event(msg, "2"))
        assert queue.empty()
        assert adapter.state.cursor == "2"
    finally:
        await adapter.terminate()
    assert adapter.client.closed


async def test_group_mentions_isolation_and_reply_target(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import At, Plain

    msg = message(threadId="room@chatroom", mentions={"state": "known", "self": True})
    adapter, queue, msg, sent = await make_adapter(serve, tmp_path, msg)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        assert event.get_group_id() == "room@chatroom"
        assert event.session.session_id == "a|group|room%40chatroom"
        assert isinstance(event.message_obj.message[0], At)
        await event.send(MessageChain([At(qq="user"), Plain("answer")]))
        assert sent[0][1] == {
            "threadId": "room@chatroom",
            "text": "answer",
            "mentions": {"userIds": ["user"]},
        }
        msg.update(id="m2", senderId="other")
        await adapter.process_native_event(native_event(msg, "2"))
        assert queue.get_nowait().session.session_id == event.session.session_id
    finally:
        await adapter.terminate()


async def test_recall_prevents_queued_reply(serve, tmp_path):
    from astrbot.api.event import MessageChain

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        await adapter.process_native_event(
            {
                "cursor": "2",
                "kind": "message.recalled",
                "data": {"messageId": "m1", "threadId": "peer", "serverId": "900"},
            }
        )
        await event.send(MessageChain().message("should not send"))
        assert event.is_stopped()
        assert not sent
        assert adapter.state.message_status("m1") == "recalled"
    finally:
        await adapter.terminate()


async def test_identity_mismatch_keeps_cursor_for_replay(serve, tmp_path):
    adapter, queue, msg, _ = await make_adapter(serve, tmp_path, message(accountId="foreign"))
    try:
        with pytest.raises(ValueError):
            await adapter.process_native_event(native_event(msg))
        assert adapter.state.cursor == "0"
        assert queue.empty()
    finally:
        await adapter.terminate()


async def test_run_reconnects_from_committed_cursor_and_stops(serve, tmp_path):
    connections = []
    resumed = asyncio.Event()
    release = asyncio.Event()
    msg = message()

    async def handler(req):
        if req.path == "/v1/status":
            return web.json_response({"serverId": "test-native-server"})
        if req.path.endswith("events/stream"):
            connections.append(req.query["after"])
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(req)
            if len(connections) == 1:
                await response.write(("data: " + json.dumps(native_event(msg)) + "\n\n").encode())
            else:
                resumed.set()
                await release.wait()
            return response
        if "/messages/" in req.path:
            return web.json_response(msg)
        return web.json_response(
            {"id": "a", "ownUsername": "bot", "authenticated": True, "connection": "online"}
        )

    url = await serve(handler)
    adapter = module("adapter").WeBridgeAdapter(
        {"id": "a", "server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    adapter.reconnect_delay = 0.001
    task = asyncio.create_task(adapter.run())
    try:
        await asyncio.wait_for(resumed.wait(), 3)
        assert connections == ["0", "1"]
        assert adapter._event_queue.qsize() == 1
        await adapter.terminate()
        await asyncio.wait_for(task, 1)
        assert adapter.client.closed
    finally:
        release.set()
        await adapter.terminate()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_plugin_registers_config_in_real_astrbot():
    from astrbot.core.platform.register import platform_cls_map, platform_registry

    module("main")
    assert "webridge" in platform_cls_map
    metadata = next(x for x in platform_registry if x.name == "webridge")
    assert metadata.adapter_display_name == "微信消息适配器"
    assert metadata.default_config_tmpl["type"] == "webridge"
    assert metadata.default_config_tmpl["id"] == "webridge"
    assert metadata.default_config_tmpl["account_id"] == ""
    assert not metadata.support_streaming_message
    assert metadata.config_metadata["account_id"]["type"] == "string"
    assert set(metadata.default_config_tmpl) == {
        "id",
        "type",
        "enable",
        "account_id",
    }


async def test_platform_icon_is_served_through_astrbot_file_api():
    import httpx
    from astrbot.core.platform.register import platform_registry
    from astrbot.dashboard.api.files import router
    from astrbot.dashboard.services.config_service import ConfigDisplayService
    from astrbot.dashboard.services.file_service import FileService
    from fastapi import FastAPI

    module("main")
    metadata = next(item for item in platform_registry if item.name == "webridge")
    service = ConfigDisplayService.__new__(ConfigDisplayService)
    service._logo_token_cache = {}
    templates = {"webridge": metadata.default_config_tmpl.copy()}
    await service.register_platform_logo(metadata, templates)
    token = templates["webridge"].get("logo_token")
    assert token, "微信消息适配器 must provide a resolvable platform icon"

    app = FastAPI()
    app.state.services = SimpleNamespace(files=FileService())
    app.include_router(router, prefix="/api/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for _ in range(2):  # The list and editor must both be able to fetch the icon.
            response = await client.get(f"/api/v1/files/tokens/{token}")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("image/png")
            assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


async def test_startup_retries_and_preserves_framework_instance_identity(serve, tmp_path):
    attempts = 0
    connected = asyncio.Event()
    release = asyncio.Event()

    async def handler(req):
        nonlocal attempts
        if req.path == "/v1/status":
            return web.json_response({"serverId": "test-native-server"})
        if req.path.endswith("events/stream"):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(req)
            connected.set()
            await release.wait()
            return response
        attempts += 1
        if attempts == 1:
            return web.Response(status=503)
        return web.json_response(
            {"id": "a", "ownUsername": "bot", "authenticated": True, "connection": "online"}
        )

    url = await serve(handler)
    adapter = module("adapter").WeBridgeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    original_identity = adapter.client_self_id
    adapter.reconnect_delay = 0.001
    task = asyncio.create_task(adapter.run())
    try:
        await asyncio.wait_for(connected.wait(), 1)
        assert attempts == 2
        assert adapter.client_self_id == original_identity
        assert adapter.client_self_id != "a"
        assert adapter.bot_self_id == "bot"
    finally:
        release.set()
        await adapter.terminate()
        await asyncio.gather(task, return_exceptions=True)


async def test_concurrent_sends_stop_after_unknown_receipt(serve, tmp_path):
    from astrbot.api.event import MessageChain

    entered = asyncio.Event()
    release = asyncio.Event()
    posts = []
    msg = message()

    async def handler(req):
        if req.method == "POST":
            posts.append(await req.json())
            entered.set()
            await release.wait()
            return web.json_response(
                {"id": "s1", "state": "needs_review", "accountId": "a", "destination": "peer"}
            )
        return web.json_response(msg)

    url = await serve(handler)
    adapter = module("adapter").WeBridgeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    first = second = None
    try:
        await adapter.process_native_event(native_event(msg))
        event = adapter._event_queue.get_nowait()
        first = asyncio.create_task(event.send(MessageChain().message("first")))
        await entered.wait()
        second = asyncio.create_task(event.send(MessageChain().message("second")))
        await asyncio.sleep(0)  # both calls pass the initial gate before first receipt
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, Exception) for result in results)
        assert len(posts) == 1
        assert adapter.state.message_status("m1") == "needs_review"
    finally:
        release.set()
        await adapter.terminate()


async def test_recall_arriving_during_source_read_blocks_submission(serve, tmp_path):
    from astrbot.api.event import MessageChain

    entered = asyncio.Event()
    release = asyncio.Event()
    msg = message()
    reads = 0
    posts = []

    async def handler(req):
        nonlocal reads
        if req.method == "POST":
            posts.append(await req.json())
            return web.json_response(
                {"id": "s", "state": "accepted", "accountId": "a", "destination": "peer"}
            )
        reads += 1
        if reads == 2:
            entered.set()
            await release.wait()
        return web.json_response(msg)  # GET snapshot predates recall

    url = await serve(handler)
    adapter = module("adapter").WeBridgeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    try:
        await adapter.process_native_event(native_event(msg))
        event = adapter._event_queue.get_nowait()
        task = asyncio.create_task(event.send(MessageChain().message("late reply")))
        await entered.wait()
        await adapter.process_native_event(
            {
                "cursor": "2",
                "kind": "message.recalled",
                "data": {"messageId": "m1", "threadId": "peer", "serverId": "900"},
            }
        )
        release.set()
        await task
        assert posts == []
        assert event.is_stopped()
    finally:
        release.set()
        await adapter.terminate()


async def test_invalid_image_output_failure_is_retained_without_send(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Image

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        with pytest.raises(ValueError):
            await event.send(MessageChain([Image.fromBytes(b"invalid image")]))
        assert not sent
        assert adapter.state.message_status("m1") == "failed"
    finally:
        await adapter.terminate()


def test_metadata_validates_with_astrbot_loader():
    from astrbot.core.star.star_manager import PluginManager
    from conftest import ROOT

    metadata = PluginManager._load_plugin_metadata(str(ROOT))
    assert metadata.name == "astrbot_plugin_webridge"
    assert metadata.display_name == "微信消息适配器"
    assert metadata.version == "1.0.0"
    assert metadata.author == "neozhu"


async def test_old_recall_identity_does_not_stop_current_event(serve, tmp_path):
    adapter, queue, msg, _ = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        await adapter.process_native_event(
            {
                "cursor": "2",
                "kind": "message.recalled",
                "data": {"messageId": "m1", "threadId": "peer", "serverId": "899"},
            }
        )
        assert not event.is_stopped()
        assert adapter.state.message_status("m1") == "queued"
    finally:
        await adapter.terminate()


async def test_stop_during_pending_send_retains_uncertainty(serve, tmp_path):
    from astrbot.api.event import MessageChain

    entered = asyncio.Event()
    release = asyncio.Event()
    msg = message()

    async def handler(req):
        if req.method == "POST":
            entered.set()
            await release.wait()
            return web.json_response(
                {"id": "s1", "state": "accepted", "accountId": "a", "destination": "peer"}
            )
        return web.json_response(msg)

    url = await serve(handler)
    adapter = module("adapter").WeBridgeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    await adapter.process_native_event(native_event(msg))
    event = adapter._event_queue.get_nowait()
    task = asyncio.create_task(event.send(MessageChain().message("pending")))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.gather(adapter.terminate(), adapter.terminate())
        assert task.done()
        assert adapter.client.closed
        path = await asyncio.to_thread(lambda: next(tmp_path.glob("*.sqlite3")))
        state = module("state").StateStore(path)
        assert state.message_status("m1") == "needs_review"
        assert state.db.execute("SELECT state FROM sends").fetchone()[0] == "needs_review"
        state.close()
    finally:
        release.set()
        await adapter.terminate()
        await asyncio.gather(task, return_exceptions=True)


async def test_send_by_persisted_session_and_foreign_account_gate(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.platform import MessageType
    from astrbot.core.platform.message_session import MessageSession

    adapter, _, _, sent = await make_adapter(serve, tmp_path)
    try:
        good = MessageSession("native-a", MessageType.FRIEND_MESSAGE, "a|private|peer")
        await adapter.send_by_session(good, MessageChain().message("proactive"))
        assert sent[0][1] == {"threadId": "peer", "text": "proactive"}
        bad = MessageSession("native-a", MessageType.FRIEND_MESSAGE, "other|private|peer")
        with pytest.raises(ValueError):
            await adapter.send_by_session(bad, MessageChain().message("bad"))
        assert len(sent) == 1
    finally:
        await adapter.terminate()


async def test_streaming_output_is_aggregated_once(serve, tmp_path):
    from astrbot.api.event import MessageChain

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()

        async def chunks():
            yield MessageChain().message("你")
            yield MessageChain().message("好")

        await event.send_streaming(chunks())
        assert [payload for _, payload in sent] == [{"threadId": "peer", "text": "你好"}]
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "body,mentions,wanted",
    [
        ("@机器人\u2005/help", {"state": "known", "self": True}, "/help"),
        ("@机器人\u2005 /help", {"state": "known", "self": True}, "/help"),
        ("@成员\u2005@机器人\u2005/help", {"state": "known", "self": True}, "/help"),
        ("hello @机器人\u2005/help", {"state": "known", "self": True}, "hello @机器人\u2005/help"),
        ("@机器人 /help", {"state": "known", "self": True}, "@机器人 /help"),
        ("@机器人\u2005/help", {"state": "unknown", "self": True}, "@机器人\u2005/help"),
        ("@成员\u2005/help", {"state": "known", "self": False}, "@成员\u2005/help"),
    ],
)
async def test_group_mention_prefix_normalization_keeps_raw_body(
    serve, tmp_path, body, mentions, wanted
):
    from astrbot.api.message_components import Plain

    msg = message(threadId="room@chatroom", body=body, mentions=mentions)
    adapter, queue, _, _ = await make_adapter(serve, tmp_path, msg, group_reply_mode="all")
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        assert event.message_str == wanted
        assert event.message_obj.raw_message["body"] == body
        assert isinstance(event.message_obj.message[-1], Plain)
        assert event.message_obj.message[-1].text == wanted
        if wanted == "/help":
            from astrbot.core.star.filter.command import CommandFilter

            command = CommandFilter("/help")
            command.handler_params = {}
            event.is_at_or_wake_command = True
            assert command.filter(event, {})
    finally:
        await adapter.terminate()


async def test_empty_output_is_no_reply_across_restart(serve, tmp_path):
    from astrbot.api.event import MessageChain

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        await queue.get_nowait().send(MessageChain().message(" \n\t"))
        assert adapter.state.message_status("m1") == "no_reply"
        assert not sent
    finally:
        await adapter.terminate()
    state = module("state").StateStore(next(tmp_path.glob("*.sqlite3")))
    try:
        assert state.message_status("m1") == "no_reply"
    finally:
        state.close()


async def test_split_group_reply_only_sends_explicit_mentions(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import At, Plain

    msg = message(threadId="room@chatroom", mentions={"state": "known", "self": True})
    adapter, queue, _, sent = await make_adapter(serve, tmp_path, msg)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        await event.send(MessageChain().message(" "))
        await event.send(MessageChain().message("first"))
        await event.send(MessageChain().message("second"))
        await event.send(MessageChain([At(qq="other"), Plain("explicit")]))
        assert [payload.get("mentions") for _, payload in sent] == [
            None,
            None,
            {"userIds": ["other"]},
        ]
        msg["recall"] = {"observedAt": "now"}
        await event.send(MessageChain().message("recalled segment"))
        assert len(sent) == 3
        assert event.is_stopped()
    finally:
        await adapter.terminate()


async def test_later_validation_failure_keeps_partial_success_and_blocks_next_segment(
    serve, tmp_path
):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Image

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        await event.send(MessageChain().message("first"))
        with pytest.raises(ValueError):
            await event.send(MessageChain([Image.fromBytes(b"invalid image")]))
        assert adapter.state.message_status("m1") == "partial"
        assert adapter.state.send_record(sent[0][0])["state"] == "accepted"
        with pytest.raises(RuntimeError, match="人工检查"):
            await event.send(MessageChain().message("third"))
        assert len(sent) == 1
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("failure_state", ["failed", "needs_review"])
async def test_later_receipt_failure_keeps_partial_success(serve, tmp_path, failure_state):
    from astrbot.api.event import MessageChain

    msg = message()
    posts = []

    async def handler(req):
        if req.method == "POST":
            posts.append(await req.json())
            return web.json_response(
                {
                    "id": "s" + str(len(posts)),
                    "accountId": "a",
                    "destination": "peer",
                    "state": "accepted" if len(posts) == 1 else failure_state,
                }
            )
        return web.json_response(msg)

    url = await serve(handler)
    adapter = module("adapter").WeBridgeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    try:
        await adapter.process_native_event(native_event(msg))
        event = adapter._event_queue.get_nowait()
        await event.send(MessageChain().message("first"))
        with pytest.raises(module("native_client").SendError):
            await event.send(MessageChain().message("second"))
        assert adapter.state.message_status("m1") == "partial"
        assert [row[0] for row in adapter.state.db.execute("SELECT state FROM sends")] == [
            "accepted",
            failure_state,
        ]
        with pytest.raises(RuntimeError, match="人工检查"):
            await event.send(MessageChain().message("third"))
        assert len(posts) == 2
    finally:
        await adapter.terminate()


async def test_unopened_proactive_send_does_not_create_outbox_entry(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.platform import MessageType
    from astrbot.core.platform.message_session import MessageSession

    adapter, _, _, sent = await make_adapter(serve, tmp_path)
    await adapter.client.close()
    session = MessageSession("native-a", MessageType.FRIEND_MESSAGE, "a|private|peer")
    try:
        with pytest.raises(ValueError, match="未连接"):
            await adapter.send_by_session(session, MessageChain().message("proactive"))
        assert adapter.state.db.execute("SELECT count(*) FROM sends").fetchone()[0] == 0
        assert not sent
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("earlier_reply", [False, True])
async def test_cancelled_waiting_segment_cannot_be_cleared_by_inflight_success(
    serve, tmp_path, earlier_reply
):
    from astrbot.api.event import MessageChain

    entered, release = asyncio.Event(), asyncio.Event()
    msg, posts = message(), []

    async def handler(req):
        if req.method == "POST":
            posts.append(await req.json())
            send_id = "s" + str(len(posts))
            if len(posts) == (2 if earlier_reply else 1):
                entered.set()
                await release.wait()
            return web.json_response(
                {"id": send_id, "state": "accepted", "accountId": "a", "destination": "peer"}
            )
        return web.json_response(msg)

    url = await serve(handler)
    adapter = module("adapter").WeBridgeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    inflight = waiting = None
    try:
        await adapter.process_native_event(native_event(msg))
        event = adapter._event_queue.get_nowait()
        if earlier_reply:
            await event.send(MessageChain().message("earlier"))
        inflight = asyncio.create_task(event.send(MessageChain().message("inflight")))
        await asyncio.wait_for(entered.wait(), 2)
        waiting = asyncio.create_task(event.send(MessageChain().message("waiting")))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert adapter.state.message_status("m1") == (
            "partial" if earlier_reply else "needs_review"
        )
        release.set()
        await inflight
        assert adapter.state.message_status("m1") == "partial"
        with pytest.raises(RuntimeError, match="人工检查"):
            await event.send(MessageChain().message("must not continue"))
        assert len(posts) == (2 if earlier_reply else 1)
    finally:
        release.set()
        await adapter.terminate()
        await asyncio.gather(
            *(task for task in (inflight, waiting) if task), return_exceptions=True
        )


@pytest.mark.parametrize(
    "payload,body", [(VALID_JPEG, ""), (VALID_PNG, None), (VALID_JPEG, "看图")]
)
async def test_private_direct_image_delivers_component_without_prompt(
    serve, tmp_path, payload, body
):
    from astrbot.api.message_components import Image

    msg = message(type=3, body=body)
    adapter, queue, _, _ = await make_adapter(serve, tmp_path, msg, media_data=payload)
    try:
        await adapter.process_native_event(native_event(msg))
        event = await asyncio.wait_for(queue.get(), 1)
        assert event.message_str == ""
        components = event.get_messages()
        assert [type(c) for c in components] == [Image]
        await adapter.process_native_event(native_event(msg, "2"))
        assert queue.empty()
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "status,code,expected_reply",
    [
        (404, "media_not_available", "图片无法读取，请重新发送。"),
        (413, "image_size_limit", "图片太大，暂时无法处理。"),
        (403, "insufficient_scope", "图片读取功能未正确配置，请联系管理员。"),
        (422, "image_format_unsupported", "暂不支持这种图片格式。"),
    ],
)
async def test_media_errors_never_send_a_reply_outside_astrbot(
    serve, tmp_path, status, code, expected_reply
):
    msg = message(type=3, body="")
    sent = []

    async def handler(req):
        if req.path.endswith("/media"):
            return web.json_response({"error": {"code": code}}, status=status)
        if "/messages/" in req.path:
            return web.json_response(msg)
        if req.method == "POST" and req.path.endswith("/sends"):
            sent.append(await req.json())
            return web.json_response(
                {"id": "s1", "state": "accepted", "accountId": "a", "destination": "peer"}
            )
        return web.Response(status=404)

    url = await serve(handler)
    adapter = module("adapter").WeBridgeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    try:
        await adapter.process_native_event(native_event(msg))
        await asyncio.gather(*list(adapter._media_tasks))
        assert adapter._event_queue.empty()
        assert sent == []
        assert adapter.state.message_status("m1") == "failed"
    finally:
        await adapter.terminate()


async def test_triggering_image_recall_during_download_silently_cancels(serve, tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    msg = message(type=3, body="")
    sent = []

    async def handler(req):
        if req.path.endswith("/media"):
            entered.set()
            await release.wait()
            return web.json_response(
                {"items": [media_asset("a1", accountId="a", message_id="m1", data=VALID_JPEG)]}
            )
        if req.method == "POST":
            sent.append(await req.json())
        return web.json_response(msg)

    url = await serve(handler)
    adapter = module("adapter").WeBridgeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    try:
        await adapter.process_native_event(native_event(msg))
        await asyncio.wait_for(entered.wait(), 1)
        await adapter.process_native_event(
            {
                "cursor": "2",
                "kind": "message.recalled",
                "data": {"messageId": "m1", "threadId": "peer", "serverId": "900"},
            }
        )
        release.set()
        await asyncio.gather(*list(adapter._media_tasks), return_exceptions=True)
        assert adapter._event_queue.empty()
        assert sent == []
        assert adapter.state.message_status("m1") == "recalled"
    finally:
        release.set()
        await adapter.terminate()


async def test_slow_media_download_does_not_block_concurrent_text_messages(serve, tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    img_msg = message(id="m1", serverId="900", type=3, body="")
    txt_msg = message(id="m2", serverId="901", type=1, body="普通文本")

    async def handler(req):
        if req.path.endswith("/messages/m1/media"):
            entered.set()
            await release.wait()
            return web.json_response(
                {"items": [media_asset("a1", accountId="a", message_id="m1", data=VALID_JPEG)]}
            )
        if req.path.endswith("/media/a1/content"):
            return web.Response(body=VALID_JPEG, content_type="image/jpeg")
        if req.path.endswith("/messages/m1"):
            return web.json_response(img_msg)
        if req.path.endswith("/messages/m2"):
            return web.json_response(txt_msg)
        return web.Response(status=404)

    url = await serve(handler)
    queue = asyncio.Queue()
    adapter = module("adapter").WeBridgeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        queue,
        state_dir=tmp_path,
    )
    await adapter.client.open()
    try:
        await adapter.process_native_event(native_event(img_msg, "1"))
        await asyncio.wait_for(entered.wait(), 1)
        await adapter.process_native_event(native_event(txt_msg, "2"))
        first_event = queue.get_nowait()
        assert first_event.message_str == "普通文本"
        release.set()
        second_event = await asyncio.wait_for(queue.get(), 1)
        assert second_event.message_str == ""
    finally:
        release.set()
        await adapter.terminate()


async def test_terminate_during_media_download_marks_needs_review_across_restart(serve, tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    msg = message(type=3, body="")

    async def handler(req):
        if req.path.endswith("/media"):
            entered.set()
            await release.wait()
        return web.json_response(msg)

    url = await serve(handler)
    adapter = module("adapter").WeBridgeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    try:
        await adapter.process_native_event(native_event(msg))
        await asyncio.wait_for(entered.wait(), 1)
        await adapter.terminate()
        state = module("state").StateStore(next(tmp_path.glob("*.sqlite3")))
        try:
            assert state.message_status("m1") == "needs_review"
        finally:
            state.close()
    finally:
        release.set()
        await adapter.terminate()
