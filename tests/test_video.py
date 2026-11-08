"""Standard Video transport against Native's v1.5.1 media/job contracts.

The small containers below exercise byte transport, not Native's codec encoder.
Recipient playback still requires a real Native/WeChat integration test.
"""

import asyncio
import base64
import hashlib
from types import SimpleNamespace

import pytest
from aiohttp import web
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Node, Nodes, Plain, Reply, Video
from conftest import module
from test_adapter import make_adapter, message, native_event, no_telemetry  # noqa: F401
from test_native_client import VALID_JPEG


def box(kind, payload):
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


VIDEO = box(b"ftyp", b"isom\0\0\0\0") + box(b"moov", b"metadata") + box(b"mdat", b"video bytes")


def video_message(**updates):
    return message(**{"type": 43, "body": "<msg><videomsg secret='metadata'/></msg>", **updates})


def video_asset(msg, data=VIDEO, **updates):
    return {
        "id": "video-" + msg["id"],
        "accountId": "a",
        "messageId": msg["id"],
        "kind": "video",
        "variant": "normal",
        "actualVariant": "normal",
        "state": "available",
        "contentType": "video/mp4",
        "byteLength": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        **updates,
    }


async def video_env(serve, tmp_path, msg=None):
    msg = msg or video_message()
    env = SimpleNamespace(
        messages={msg["id"]: msg},
        assets={},
        contents={},
        paths=[],
        uploads=[],
        posts=[],
        caps={"mediaUploadMaxBytes": 4294967279, "mediaSendMaxBytes": {"video": 4294967279}},
        outcome="accepted",
        job_override={},
        transfer=False,
        hooks={},
    )
    asset = video_asset(msg)
    env.assets[msg["id"]] = [asset]
    env.contents[asset["id"]] = VIDEO

    async def custom_serve(fallback):
        async def handler(req):
            env.paths.append((req.method, req.path))
            parts = req.path.split("/")
            if req.path.endswith("/capabilities"):
                return web.json_response(env.caps)
            if req.method == "GET" and req.path.endswith("/media"):
                return web.json_response({"items": env.assets.get(parts[-2], [])})
            if req.method == "POST" and req.path.endswith("/media-transfers"):
                env.transfer = await req.json()
                for a in env.assets[parts[-2]]:
                    if a["id"] == env.transfer["assetId"]:
                        a["state"] = "available"
                return web.json_response(
                    {
                        "id": "download-1",
                        "accountId": "a",
                        "kind": "download",
                        "assetId": env.transfer["assetId"],
                        "state": "completed",
                    },
                    status=202,
                )
            if req.method == "GET" and req.path.endswith("/content"):
                hook = env.hooks.get("content")
                if hook:
                    await hook()
                return web.Response(
                    body=env.contents[parts[-2]], content_type="application/octet-stream"
                )
            if req.method == "GET" and "/messages/" in req.path:
                return web.json_response(env.messages.get(parts[-1], msg))
            if req.path.endswith("/uploads"):
                form = await req.multipart()
                item = await form.next()
                data = bytes(await item.read())
                assert item.name == "file" and await form.next() is None
                env.uploads.append((item.filename, item.headers["Content-Type"], data))
                hook = env.hooks.get("upload")
                if hook:
                    await hook()
                return web.json_response(
                    {
                        "id": f"source-{len(env.uploads)}",
                        "byteLength": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    },
                    status=201,
                )
            if req.method == "POST" and req.path.endswith("/sends"):
                payload = await req.json()
                if "text" in payload:
                    return await fallback(req)
                env.posts.append((req.headers["Idempotency-Key"], payload))
                return web.json_response(
                    {
                        "id": f"job-{len(env.posts)}",
                        "accountId": "a",
                        "kind": "send",
                        "threadId": msg["threadId"],
                        "sourceId": payload["sourceId"],
                        "state": "queued",
                        **env.job_override,
                    },
                    status=202,
                )
            if "/jobs/" in req.path:
                return web.json_response(
                    {
                        "id": parts[-1],
                        "accountId": "a",
                        "kind": "send",
                        "threadId": msg["threadId"],
                        "sourceId": f"source-{len(env.uploads)}",
                        "state": env.outcome,
                        "serverMessageId": "901",
                        "result": {"messageIds": ["out-1"], "profileProjection": "complete"},
                        **env.job_override,
                    }
                )
            return await fallback(req)

        return await serve(handler)

    env.adapter, env.queue, _, env.sent = await make_adapter(custom_serve, tmp_path, msg)
    env.adapter.client.poll_interval = 0.001
    env.adapter.client.video_send_timeout = 1
    return env


async def deliver(env, msg, cursor="1"):
    await env.adapter.process_native_event(native_event(msg, cursor))
    await asyncio.gather(*list(env.adapter._media_tasks))


@pytest.mark.parametrize("thread", ["peer", "room@chatroom"])
async def test_incoming_video_is_ignored_without_download_or_handoff(serve, tmp_path, thread):
    msg = video_message(threadId=thread)
    env = await video_env(serve, tmp_path, msg)
    try:
        await deliver(env, msg)
        await deliver(env, msg, "2")
        assert env.queue.empty() and not env.adapter._media_tasks
        assert env.adapter.state.message_status(msg["id"]) == "ignored"
        assert env.uploads == env.posts == env.sent == []
        assert not any(p.endswith(("/media", "/content", "/media-transfers")) for _, p in env.paths)
    finally:
        await env.adapter.terminate()


async def test_video_quote_only_delivers_user_text_and_reference(serve, tmp_path):
    original = video_message(id="original", serverId="800")
    msg = message(
        type=49,
        appMessageType=57,
        body="这个片段",
        quote={
            "serverId": "800",
            "senderId": "user",
            "type": 43,
            "state": "unsupported",
        },
    )
    env = await video_env(serve, tmp_path, original)
    env.messages[msg["id"]] = msg
    env.adapter.state.accept("1", original["id"], original["threadId"], "800", "ignored")
    try:
        await deliver(env, msg, "2")
        event = env.queue.get_nowait()
        assert event.message_str == "这个片段"
        assert [type(c) for c in event.get_messages()] == [Reply, Plain]
        assert event.get_messages()[0].message_str == "[视频]"
        assert not env.adapter._media_tasks
        assert not any(p.endswith(("/media", "/content", "/media-transfers")) for _, p in env.paths)
    finally:
        await env.adapter.terminate()


async def test_video_over_old_fifty_mib_cap_uploads_exact_source(serve, tmp_path):
    source = VIDEO + box(b"free", bytes(51 * 1024 * 1024))
    path = tmp_path / "large.mp4"
    path.write_bytes(source)
    env = await video_env(serve, tmp_path, message())
    try:
        await env.adapter.send_chain("peer", MessageChain([Video.fromFileSystem(path)]))
        assert len(env.uploads) == len(env.posts) == 1
        assert env.uploads[0][2] == source
        assert env.posts[0][1]["kind"] == "video"
    finally:
        await env.adapter.terminate()


@pytest.mark.parametrize("quoted", [False, True])
async def test_pdf_is_no_longer_downloaded(serve, tmp_path, quoted):
    msg = message(
        type=49,
        appMessageType=57 if quoted else 6,
        body="报告",
        quote={
            "type": 49,
            "appMessageType": 6,
            "state": "unsupported",
            "embeddedText": "report.pdf",
        }
        if quoted
        else None,
    )
    env = await video_env(serve, tmp_path, msg)
    try:
        await deliver(env, msg)
        assert not any(p.endswith(("/media", "/content", "/media-transfers")) for _, p in env.paths)
        assert env.queue.empty() is (not quoted)
        assert not hasattr(env.adapter.client, "fetch_pdf")
        if quoted:
            assert env.queue.get_nowait().message_str == "报告"
    finally:
        await env.adapter.terminate()


@pytest.mark.parametrize(
    "source", ["path", "bare_path", "base64", "data", "url", "url_field", "path_field"]
)
async def test_video_sources_upload_identical_bytes_and_send_video(
    serve, tmp_path, public_image_url, source
):
    path = tmp_path / "测试 clip.mp4"
    path.write_bytes(VIDEO)
    encoded = base64.b64encode(VIDEO).decode()

    async def download(req):
        assert "Authorization" not in req.headers and "Cookie" not in req.headers
        return web.Response(body=VIDEO)

    url = await public_image_url(download)
    video = {
        "path": Video.fromFileSystem(path),
        "bare_path": Video(file=str(path)),
        "base64": Video.fromBase64(encoded),
        "data": Video(file="data:video/mp4;base64," + encoded),
        "url": Video.fromURL(url),
        "url_field": Video(file="", url=url),
        "path_field": Video(file="missing-file", path=str(path)),
    }[source]
    env = await video_env(serve, tmp_path, message())
    try:
        await env.adapter.send_chain("peer", MessageChain([video]))
        assert env.uploads == [("video.mp4", "video/mp4", VIDEO)]
        assert env.posts[0][1] == {"threadId": "peer", "kind": "video", "sourceId": "source-1"}
        assert len(env.posts) == 1
        assert env.adapter.state.send_record(env.posts[0][0])["state"] == "accepted"
        assert env.paths.index(("GET", "/v1/accounts/a/capabilities")) < env.paths.index(
            ("POST", "/v1/accounts/a/uploads")
        )
    finally:
        await env.adapter.terminate()


@pytest.mark.parametrize(
    "caps",
    [
        {},
        {"mediaUploadMaxBytes": 100, "mediaSendMaxBytes": {"video": 1}},
        {"mediaUploadMaxBytes": 1, "mediaSendMaxBytes": {"video": 100}},
        {"mediaUploadMaxBytes": True, "mediaSendMaxBytes": {"video": True}},
    ],
)
async def test_video_capability_limits_checked_before_staging(serve, tmp_path, caps):
    env = await video_env(serve, tmp_path)
    env.caps = caps
    try:
        with pytest.raises(ValueError):
            await env.adapter.send_chain(
                "peer", MessageChain([Video.fromBase64(base64.b64encode(VIDEO).decode())])
            )
        assert env.uploads == env.posts == []
    finally:
        await env.adapter.terminate()


@pytest.mark.parametrize(
    "outcome,status",
    [
        ("failed", "failed"),
        ("partial", "partial"),
        ("queued", "needs_review"),
        ("needs_review", "needs_review"),
    ],
)
async def test_unconfirmed_video_is_retained_without_resubmission(serve, tmp_path, outcome, status):
    env = await video_env(serve, tmp_path)
    env.outcome = outcome
    if outcome == "queued":
        env.adapter.client.video_send_timeout = 0.05
    try:
        with pytest.raises(module("native_client").SendError):
            await env.adapter.send_chain(
                "peer", MessageChain([Video.fromBase64(base64.b64encode(VIDEO).decode())])
            )
        assert len(env.uploads) == len(env.posts) == 1
        assert env.adapter.state.send_record(env.posts[0][0])["state"] == status
    finally:
        await env.adapter.terminate()


@pytest.mark.parametrize(
    "invalid", [b"<html>error</html>", VALID_JPEG, b"\0\0\0\x18ftypisomsynthetic", VIDEO[:-1]]
)
async def test_invalid_container_never_uploaded(serve, tmp_path, invalid):
    env = await video_env(serve, tmp_path)
    try:
        with pytest.raises(ValueError):
            await env.adapter.send_chain(
                "peer", MessageChain([Video.fromBase64(base64.b64encode(invalid).decode())])
            )
        assert env.uploads == env.posts == []
    finally:
        await env.adapter.terminate()


async def test_parser_forward_nodes_send_contents_in_order(serve, tmp_path):
    env = await video_env(serve, tmp_path, message())
    chain = MessageChain(
        [
            Nodes(
                [
                    Node(content=[Image.fromBytes(VALID_JPEG)]),
                    Node(content=[Video.fromBase64(base64.b64encode(VIDEO).decode())]),
                    Node(content=[Plain("first")]),
                    Node(content=[Plain("second")]),
                ]
            )
        ]
    )
    try:
        await env.adapter.send_chain("peer", chain)
        assert [p["kind"] for _, p in env.posts] == ["image", "video"]
        assert [p["text"] for _, p in env.sent] == ["first", "second"]
        assert env.uploads[0][2] == VALID_JPEG
        assert env.uploads[1][2] == VIDEO
    finally:
        await env.adapter.terminate()


@pytest.mark.parametrize("proactive", [False, True])
@pytest.mark.parametrize("thread", ["peer", "room@chatroom"])
async def test_video_reply_and_saved_session_send(serve, tmp_path, proactive, thread):
    msg = message(threadId=thread)
    env = await video_env(serve, tmp_path, msg)
    try:
        await deliver(env, msg)
        event = env.queue.get_nowait()
        chain = MessageChain([Video.fromBase64(base64.b64encode(VIDEO).decode())])
        if proactive:
            await env.adapter.send_by_session(event.session, chain)
        else:
            await event.send(chain)
            assert event._has_send_oper
            assert env.adapter.state.message_status(msg["id"]) == "replied"
        assert env.posts[0][1]["threadId"] == thread
        assert env.posts[0][1]["kind"] == "video"
        assert len(env.posts) == 1
    finally:
        await env.adapter.terminate()


@pytest.mark.parametrize(
    "image_format,suffix,mime",
    [
        ("PNG", "png", "image/png"),
        ("JPEG", "jpg", "image/jpeg"),
    ],
)
async def test_large_images_use_image_messages_and_upload_full_source(
    serve, tmp_path, image_format, suffix, mime
):
    import io
    import random

    from PIL import Image as PILImage

    # Real encoded images larger than Native's 65,520-byte image-copy limit.
    image = PILImage.frombytes("RGB", (512, 512), random.Random(42).randbytes(512 * 512 * 3))
    buffer = io.BytesIO()
    image.save(buffer, format=image_format)
    original = buffer.getvalue()
    assert len(original) > 65520
    path = tmp_path / ("原始图片." + suffix)
    path.write_bytes(original)
    env = await video_env(serve, tmp_path, message())
    try:
        await env.adapter.send_chain("peer", MessageChain([Image.fromFileSystem(path)]))
        assert len(env.uploads) == len(env.posts) == 1
        filename, uploaded_mime, uploaded = env.uploads[0]
        assert (filename, uploaded_mime) == ("image." + suffix, mime)
        assert hashlib.sha256(uploaded).digest() == hashlib.sha256(original).digest()
        assert uploaded == original
        with PILImage.open(io.BytesIO(uploaded)) as received:
            assert received.size == (512, 512)
        assert env.posts[0][1]["kind"] == "image"
        assert env.sent == []  # Exactly one image send, no file message or extra text.
    finally:
        await env.adapter.terminate()


async def test_video_recall_during_upload_blocks_submission(serve, tmp_path):
    msg = message()
    env = await video_env(serve, tmp_path, msg)
    await deliver(env, msg)
    event = env.queue.get_nowait()

    async def recall():
        msg["recall"] = {"observedAt": "now"}

    env.hooks["upload"] = recall
    try:
        with pytest.raises(ValueError):
            await event.send(MessageChain([Video.fromBase64(base64.b64encode(VIDEO).decode())]))
        assert len(env.uploads) == 1 and env.posts == []
        assert env.adapter.state.message_status(msg["id"]) == "recalled"
    finally:
        await env.adapter.terminate()
