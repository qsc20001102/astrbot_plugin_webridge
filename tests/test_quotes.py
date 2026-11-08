import pytest
from aiohttp import web
from test_adapter import (
    make_adapter,
    message,
    native_event,
    no_telemetry,  # noqa: F401
)
from test_native_client import VALID_JPEG, client, media_asset, receipt


@pytest.mark.parametrize("thread", ["peer", "room@chatroom"])
async def test_reply_component_sends_quote_without_adding_a_mention(serve, tmp_path, thread):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain, Reply

    adapter, queue, msg, sent = await make_adapter(
        serve, tmp_path, message(threadId=thread, mentions={"state": "known", "self": True})
    )
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        await event.send(MessageChain([Reply(id="m1", message_str="不可信正文"), Plain("同意")]))
        expected = {
            "threadId": thread,
            "text": "同意",
            "quote": {"messageId": "m1", "serverId": "900"},
        }
        assert sent[0][1] == expected
        assert adapter.state.message_status("m1") == "replied"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "recipient,mentions",
    [
        ("user", {"userIds": ["user"]}),
        ("other", {"userIds": ["other"]}),
        ("all", {"all": True}),
    ],
)
async def test_quote_preserves_explicit_group_mentions(serve, tmp_path, recipient, mentions):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import At, Plain, Reply

    adapter, queue, msg, sent = await make_adapter(
        serve,
        tmp_path,
        message(threadId="room@chatroom", mentions={"state": "known", "self": True}),
    )
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        await event.send(MessageChain([Reply(id="m1"), At(qq=recipient), Plain("同意")]))
        assert sent[0][1]["mentions"] == mentions
        assert sent[0][1]["quote"] == {"messageId": "m1", "serverId": "900"}
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("case", ["multiple", "empty_id", "too_long"])
async def test_invalid_quote_chain_is_rejected_before_submission(serve, tmp_path, case):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain, Reply

    parts = {
        "multiple": [Reply(id="m1"), Reply(id="m2"), Plain("回复")],
        "empty_id": [Reply(id=""), Plain("回复")],
        "too_long": [Reply(id="m1"), Plain("中" * 5462)],
    }[case]
    adapter, queue, msg, sent = await make_adapter(
        serve,
        tmp_path,
        message(threadId="room@chatroom", mentions={"state": "known", "self": True}),
    )
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        with pytest.raises(ValueError):
            await event.send(MessageChain(parts))
        assert sent == []
        assert adapter.state.db.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == 0
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "changes",
    [
        {"accountId": "foreign"},
        {"threadId": "other"},
        {"id": "wrong"},
        {"serverId": None},
        {"serverId": 900},
        {"recall": {"observedAt": "now"}},
    ],
)
async def test_quote_target_must_be_available_in_same_account_and_chat(serve, tmp_path, changes):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain, Reply

    adapter, _, _, sent = await make_adapter(serve, tmp_path)
    target = message(**changes)

    async def read(message_id):
        return target

    adapter.client.message = read
    try:
        with pytest.raises(ValueError):
            await adapter.send_chain("peer", MessageChain([Reply(id="m1"), Plain("回复")]))
        assert sent == []
        assert adapter.state.db.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == 0
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("mentions", [None, {"userIds": ["other"]}, {"all": True}])
async def test_native_quote_preserves_server_id_and_recovers_original_receipt(serve, mentions):
    requests = []

    async def handler(req):
        requests.append(req.method)
        if req.method == "POST":
            expected = {
                "threadId": "room@chatroom",
                "text": "回复",
                "quote": {"messageId": "opaque/id", "serverId": "18446744073709551615"},
            }
            if mentions:
                expected["mentions"] = mentions
            assert await req.json() == expected
            return web.json_response({"error": {"code": "timeout"}}, status=503)
        assert req.query["key"] == "original-quote-key"
        return web.json_response(receipt())

    async with client(await serve(handler)) as c:
        result = await c.send_text(
            "room@chatroom",
            "回复",
            "original-quote-key",
            mentions=mentions,
            quote={
                "messageId": "opaque/id",
                "serverId": "18446744073709551615",
            },
        )
    assert result["state"] == "accepted"
    assert requests == ["POST", "GET"]


async def test_source_recall_during_other_quote_read_still_prevents_reply(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain, Reply

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    original = adapter.client.message

    async def read(message_id):
        if message_id == "m2":
            adapter.state.recall("peer", "900", "m1")
            return message(id="m2", serverId="901")
        return await original(message_id)

    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        adapter.client.message = read
        await event.send(MessageChain([Reply(id="m2"), Plain("回复")]))
        assert sent == []
        assert adapter.state.message_status("m1") == "recalled"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "state,expected",
    [
        ("embedded", "引用原文"),
        ("recalled", "撤回"),
        ("missing", "不可用"),
        ("unsupported", "不支持"),
        ("unknown", "不可用"),
    ],
)
async def test_incoming_quote_exposes_only_available_embedded_text(
    serve, tmp_path, state, expected
):
    from astrbot.api.message_components import Reply

    msg = message(
        type=49,
        appMessageType=57,
        body="你的看法呢？",
        quote={
            "serverId": "18446744073709551615",
            "type": 1,
            "state": state,
            "senderId": "author",
            "embeddedText": "引用原文",
        },
    )
    adapter, queue, _, _ = await make_adapter(serve, tmp_path, msg)
    try:
        await adapter.process_native_event(native_event(msg))
        assert not queue.empty()
        event = queue.get_nowait()
        quote = next(
            component for component in event.get_messages() if isinstance(component, Reply)
        )
        assert expected in quote.message_str
        if state != "embedded":
            assert "引用原文" not in quote.message_str
        assert event.message_str == "你的看法呢？"
        assert event.message_obj.raw_message["quote"]["serverId"] == "18446744073709551615"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "kind,subtype,mentioned,expected",
    [
        (49, 57, True, True),
        (49, 57, False, True),
        (49, 5, True, False),
    ],
)
async def test_incoming_quotes_are_not_filtered_by_mentions(
    serve,
    tmp_path,
    kind,
    subtype,
    mentioned,
    expected,
):
    msg = message(
        threadId="room@chatroom",
        type=kind,
        appMessageType=subtype,
        mentions={"state": "known", "self": mentioned},
    )
    adapter, queue, _, _ = await make_adapter(serve, tmp_path, msg)
    try:
        await adapter.process_native_event(native_event(msg))
        assert (not queue.empty()) is expected
    finally:
        await adapter.terminate()


async def test_quote_can_target_another_saved_message(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain, Reply

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    original = adapter.client.message

    async def read(message_id):
        if message_id == "m2":
            return message(id="m2", serverId="18446744073709551615")
        return await original(message_id)

    try:
        await adapter.process_native_event(native_event(msg))
        adapter.client.message = read
        await queue.get_nowait().send(MessageChain([Reply(id="m2"), Plain("回复旧消息")]))
        assert sent[0][1]["quote"] == {
            "messageId": "m2",
            "serverId": "18446744073709551615",
        }
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("use_recall_event", [True, False])
async def test_embedded_reference_recall_cancels_reply(serve, tmp_path, use_recall_event):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain

    msg = message(
        type=49,
        appMessageType=57,
        body="解释一下",
        quote={
            "serverId": "899",
            "type": 1,
            "state": "embedded",
            "senderId": "author",
            "embeddedText": "随后撤回的敏感文本",
        },
    )
    adapter, queue, _, sent = await make_adapter(serve, tmp_path, msg)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        if use_recall_event:
            await adapter.process_native_event(
                {
                    "cursor": "2",
                    "kind": "message.recalled",
                    "data": {"threadId": "peer", "serverId": "899", "messageId": "original"},
                }
            )
            assert event.is_stopped()
        else:
            msg["quote"] = {"serverId": "899", "type": 1, "state": "recalled"}
        await event.send(MessageChain([Plain("根据引用文本生成的旧回答")]))
        assert sent == []
        assert adapter.state.message_status("m1") == "no_reply"
    finally:
        await adapter.terminate()


async def test_private_quoted_image_resolves_via_current_message_id(serve, tmp_path):
    import asyncio

    from astrbot.api.message_components import Image, Plain, Reply

    media_paths = []
    msg = message(
        id="quote-msg-1",
        type=49,
        appMessageType=57,
        body="帮我看看这张图",
        quote={
            "serverId": "original-server-id-899",
            "type": 3,
            "state": "embedded",
            "senderId": "author",
            "embeddedImage": True,
            "embeddedText": "",
        },
    )

    async def custom_serve(default_handler):
        async def handler(req):
            if req.path.endswith("/media"):
                media_paths.append(req.path)
            return await default_handler(req)

        return await serve(handler)

    adapter, queue, _, _ = await make_adapter(custom_serve, tmp_path, msg)
    try:
        await adapter.process_native_event(native_event(msg))
        event = await asyncio.wait_for(queue.get(), 1)
        assert media_paths == ["/v1/accounts/a/messages/quote-msg-1/media"]
        assert event.message_str == "帮我看看这张图"
        components = event.get_messages()
        assert [type(c) for c in components] == [Reply, Plain, Image]
        assert components[0].message_str == "[图片]"
        assert "不支持" not in components[0].message_str
        assert not any(isinstance(c, Image) for c in components[0].chain)
    finally:
        await adapter.terminate()


async def make_quote_fallback_adapter(
    serve,
    tmp_path,
    *,
    thread="peer",
    status=409,
    code="media_metadata_invalid",
    original_media_error=None,
    download_hook=None,
):
    original = message(
        id="original-native-id",
        serverId="899",
        senderId="author",
        type=3,
        threadId=thread,
        body=None,
    )
    msg = message(
        id="quote-msg",
        threadId=thread,
        type=49,
        appMessageType=57,
        body="帮我分析这张图",
        mentions={"state": "known", "self": True},
        quote={
            "serverId": "899",
            "senderId": "author",
            "type": 3,
            "state": "embedded",
            "embeddedImage": True,
        },
    )
    requests = []

    async def custom_serve(default_handler):
        async def handler(req):
            requests.append(req.path)
            if req.path.endswith("/messages/quote-msg/media"):
                return web.json_response({"error": {"code": code}}, status=status)
            if req.path.endswith("/messages/original-native-id"):
                return web.json_response(original)
            if req.path.endswith("/messages/original-native-id/media"):
                if original_media_error:
                    error_status, error_code = original_media_error
                    return web.json_response({"error": {"code": error_code}}, status=error_status)
                return web.json_response(
                    {
                        "items": [
                            media_asset("asset-1", accountId="a", message_id="original-native-id")
                        ]
                    }
                )
            if req.path.endswith("/media/asset-1/content") and download_hook:
                await download_hook(original, msg)
            return await default_handler(req)

        return await serve(handler)

    adapter, queue, _, sent = await make_adapter(custom_serve, tmp_path, msg)
    adapter.state.accept("1", original["id"], thread, "899", "replied")
    return adapter, queue, msg, original, sent, requests


@pytest.mark.parametrize("thread", ["peer", "room@chatroom"])
async def test_quoted_image_metadata_error_uses_verified_original(serve, tmp_path, thread):
    import asyncio
    import base64

    from astrbot.api.message_components import Image, Reply

    adapter, queue, msg, _, sent, requests = await make_quote_fallback_adapter(
        serve, tmp_path, thread=thread
    )
    try:
        await adapter.process_native_event(native_event(msg, "2"))
        await asyncio.gather(*list(adapter._media_tasks))
        assert queue.qsize() == 1
        event = queue.get_nowait()
        assert event.message_str == "帮我分析这张图"
        images = [part for part in event.get_messages() if isinstance(part, Image)]
        assert len(images) == 1
        assert images[0].file == "base64://" + base64.b64encode(VALID_JPEG).decode()
        reply = next(part for part in event.get_messages() if isinstance(part, Reply))
        assert not any(isinstance(part, Image) for part in reply.chain)
        media_paths = [path for path in requests if path.endswith("/media")]
        assert media_paths == [
            "/v1/accounts/a/messages/quote-msg/media",
            "/v1/accounts/a/messages/original-native-id/media",
        ]
        assert sent == []
        await adapter.process_native_event(native_event(msg, "3"))
        assert queue.empty()
        assert [path for path in requests if path.endswith("/media")] == media_paths
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "changes",
    [
        {"accountId": "other"},
        {"threadId": "other-peer"},
        {"id": "other-id"},
        {"serverId": "other-server"},
        {"senderId": "other-author"},
        {"type": 1},
    ],
)
async def test_quoted_image_fallback_rejects_original_identity_changes(serve, tmp_path, changes):
    import asyncio

    adapter, queue, msg, original, sent, requests = await make_quote_fallback_adapter(
        serve, tmp_path
    )
    original.update(changes)
    try:
        await adapter.process_native_event(native_event(msg, "2"))
        await asyncio.gather(*list(adapter._media_tasks))
        from astrbot.api.message_components import Image

        event = queue.get_nowait()
        assert event.message_str == msg["body"]
        assert not any(isinstance(part, Image) for part in event.get_messages())
        assert event.get_extra("webridge_media_error")
        assert sent == []
        assert not any(path.endswith("/original-native-id/media") for path in requests)
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("known_elsewhere", [False, True])
async def test_quoted_image_fallback_requires_original_in_same_chat(
    serve, tmp_path, known_elsewhere
):
    import asyncio

    adapter, queue, msg, _, sent, requests = await make_quote_fallback_adapter(serve, tmp_path)
    msg["quote"]["serverId"] = "unknown-server"
    if known_elsewhere:
        adapter.state.accept("2", "foreign-original", "another-chat", "unknown-server", "ignored")
    try:
        await adapter.process_native_event(native_event(msg, "3"))
        await asyncio.gather(*list(adapter._media_tasks))
        from astrbot.api.message_components import Image

        event = queue.get_nowait()
        assert event.message_str == msg["body"]
        assert not any(isinstance(part, Image) for part in event.get_messages())
        assert event.get_extra("webridge_media_error")
        assert sent == []
        assert not any(path.endswith("/original-native-id") for path in requests)
        assert not any("unknown-server" in path for path in requests)
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("after_dispatch", [False, True])
async def test_quoted_image_fallback_original_recall_suppresses_reply(
    serve, tmp_path, after_dispatch
):
    import asyncio

    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain

    adapter, queue, msg, original, sent, _ = await make_quote_fallback_adapter(serve, tmp_path)
    if not after_dispatch:
        original["recall"] = {"observedAt": "now"}
    try:
        await adapter.process_native_event(native_event(msg, "2"))
        await asyncio.gather(*list(adapter._media_tasks))
        if after_dispatch:
            assert queue.qsize() == 1
            event = queue.get_nowait()
            original["recall"] = {"observedAt": "now"}
            await event.send(MessageChain([Plain("根据原图生成的回答")]))
            assert event.is_stopped()
        assert queue.empty()
        assert sent == []
        assert adapter.state.message_status(msg["id"]) == "no_reply"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "status,code",
    [
        (403, "insufficient_scope"),
        (404, "media_not_cached"),
        (500, "media_metadata_invalid"),
        (409, "media_integrity_failed"),
    ],
)
async def test_quoted_image_fallback_does_not_mask_other_native_errors(
    serve, tmp_path, status, code
):
    import asyncio

    adapter, queue, msg, _, sent, requests = await make_quote_fallback_adapter(
        serve, tmp_path, status=status, code=code
    )
    try:
        await adapter.process_native_event(native_event(msg, "2"))
        await asyncio.gather(*list(adapter._media_tasks))
        from astrbot.api.message_components import Image

        event = queue.get_nowait()
        assert event.message_str == msg["body"]
        assert not any(isinstance(part, Image) for part in event.get_messages())
        assert event.get_extra("webridge_media_error")
        assert sent == []
        assert not any(path.endswith("/original-native-id") for path in requests)
    finally:
        await adapter.terminate()


async def test_quoted_image_fallback_media_recall_keeps_trigger_unrecalled(serve, tmp_path):
    import asyncio

    adapter, queue, msg, _, sent, _ = await make_quote_fallback_adapter(
        serve, tmp_path, original_media_error=(410, "message_recalled")
    )
    try:
        await adapter.process_native_event(native_event(msg, "2"))
        await asyncio.gather(*list(adapter._media_tasks))
        assert queue.empty()
        assert sent == []
        assert adapter.state.message_status(msg["id"]) == "no_reply"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("changed_source", ["original", "quote", "trigger"])
async def test_quoted_image_fallback_rechecks_sources_after_download(
    serve, tmp_path, changed_source
):
    import asyncio

    async def change_source(original, msg):
        if changed_source == "original":
            original["recall"] = {"observedAt": "now"}
        elif changed_source == "quote":
            msg["quote"]["serverId"] = "another-image"
        else:
            msg["recall"] = {"observedAt": "now"}

    adapter, queue, msg, _, sent, _ = await make_quote_fallback_adapter(
        serve, tmp_path, download_hook=change_source
    )
    try:
        await adapter.process_native_event(native_event(msg, "2"))
        await asyncio.gather(*list(adapter._media_tasks))
        assert queue.empty()
        assert sent == []
        assert adapter.state.message_status(msg["id"]) == (
            "recalled" if changed_source == "trigger" else "no_reply"
        )
    finally:
        await adapter.terminate()


async def test_quoted_image_fallback_obeys_media_deadline(serve, tmp_path):
    import asyncio

    release = asyncio.Event()

    async def slow_download(original, msg):
        await release.wait()

    adapter, queue, msg, _, sent, _ = await make_quote_fallback_adapter(
        serve, tmp_path, download_hook=slow_download
    )
    adapter.client.media_timeout = 0.05
    try:
        await adapter.process_native_event(native_event(msg, "2"))
        await asyncio.wait_for(asyncio.gather(*list(adapter._media_tasks)), 0.5)
        from astrbot.api.message_components import Image

        event = queue.get_nowait()
        assert event.message_str == msg["body"]
        assert not any(isinstance(part, Image) for part in event.get_messages())
        assert event.get_extra("webridge_media_error")
        assert sent == []
        assert adapter.state.message_status(msg["id"]) == "queued"
    finally:
        release.set()
        await adapter.terminate()
