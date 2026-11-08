"""Outbound images against the v1.4.0 upload/source/send-job HTTP contract."""

import asyncio
import base64
import hashlib
import io
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Image, Plain, Reply
from conftest import module
from test_adapter import message
from test_native_client import VALID_JPEG, member_lookup


async def image_adapter(serve, tmp_path, *, thread="peer", outcome="accepted", fail_at=1):
    msg = message(threadId=thread)
    requests, uploads, posts = [], [], []
    jobs = {}

    async def handler(req):
        requests.append((req.method, req.path))
        if "/messages/" in req.path:
            return web.json_response(msg)
        if "/members/" in req.path:
            return web.json_response(member_lookup(req.path.rsplit("/", 1)[-1]))
        if req.path.endswith("/uploads"):
            form = await req.multipart()
            file = await form.next()
            assert file.name == "file"
            assert file.filename == "image.jpg"
            assert file.headers["Content-Type"] == "image/jpeg"
            data = bytes(await file.read())
            assert data == VALID_JPEG
            assert await form.next() is None
            uploads.append(data)
            if outcome in {"upload400", "upload503"}:
                return web.json_response({}, status=400 if outcome == "upload400" else 503)
            if outcome == "upload_disconnect":
                req.transport.close()
                return web.Response()
            if outcome == "upload_timeout":
                await asyncio.sleep(0.15)
            return web.json_response(
                {
                    "id": f"source-{len(uploads)}",
                    "name": file.filename,
                    "mediaType": "image/jpeg",
                    "byteLength": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                },
                status=201,
            )
        if req.method == "POST" and req.path.endswith("/sends"):
            payload = await req.json()
            key = req.headers["Idempotency-Key"]
            assert key not in [entry[0] for entry in posts]
            posts.append((key, payload))
            if payload.get("kind") == "image":
                assert set(payload) == {"threadId", "kind", "sourceId"}
                state = outcome if len(uploads) == fail_at else "accepted"
                if state in {"http400", "http408", "http503", "lost"}:
                    status = {"http400": 400, "http408": 408, "http503": 503, "lost": 503}[state]
                else:
                    status = 202
                job = {
                    "id": f"job-{len(uploads)}",
                    "accountId": "a",
                    "kind": "send",
                    "threadId": thread,
                    "sourceId": payload["sourceId"],
                    "state": "queued" if state in {"poll", "http503", "read503"} else state,
                    "serverMessageId": "901",
                    "result": {"messageIds": ["out-1"], "profileProjection": "complete"},
                }
                jobs[job["id"]] = job
                if state == "projection_unknown":
                    job["state"] = "accepted"
                    job["result"]["profileProjection"] = "unknown"
                if state == "missing_receipt":
                    job["state"] = "accepted"
                    job["result"] = {}
                if state == "http400":
                    return web.json_response(
                        {"error": {"code": "unsupported_media"}}, status=status
                    )
                if status != 202:
                    return web.json_response({}, status=status)
                return web.json_response(job, status=202)
            return web.json_response(
                {
                    "id": f"text-{len(posts)}",
                    "accountId": "a",
                    "destination": thread,
                    "state": "accepted",
                }
            )
        if req.path.endswith("/sends/by-idempotency-key"):
            assert req.query["key"] == posts[-1][0]
            if outcome in {"lost", "http408"}:
                return web.json_response({}, status=404)
            return web.json_response(jobs[f"job-{len(uploads)}"])
        if "/jobs/" in req.path:
            job = dict(jobs[req.path.rsplit("/", 1)[-1]])
            if outcome in {"poll", "http503"}:
                job["state"] = "accepted"
            if outcome == "read503":
                return web.json_response({}, status=503)
            return web.json_response(job)
        return web.json_response({}, status=404)

    url = await serve(handler)
    adapter = module("adapter").WeBridgeAdapter(
        {"id": "native-a", "server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    adapter.bot_self_id = "bot"
    adapter.client.poll_interval = 0.001
    adapter.client.send_timeout = 1
    adapter.client.video_send_timeout = 1
    # Seed the incoming event without invoking unrelated inbound-media policy.
    adapter.state.accept("1", "m1", thread, "900", "queued")
    converted = module("event").convert_message(msg, adapter.native_config, "bot")
    event = module("event").WeBridgeEvent(converted, adapter)
    return adapter, event, requests, uploads, posts, jobs


@pytest.mark.parametrize(
    "source", ["bytes", "path", "url", "base64", "data", "io", "bare", "plain_path", "url_field"]
)
async def test_single_image_sources(serve, tmp_path, public_image_url, source):
    encoded = base64.b64encode(VALID_JPEG).decode()
    path = tmp_path / "测试 image.jpg"
    path.write_bytes(VALID_JPEG)

    async def download(req):
        assert "Authorization" not in req.headers
        assert "Cookie" not in req.headers
        return web.Response(body=VALID_JPEG)

    url = await public_image_url(download)
    image = {
        "bytes": Image.fromBytes(VALID_JPEG),
        "path": Image.fromFileSystem(path),
        "url": Image.fromURL(url),
        "base64": Image.fromBase64(encoded),
        "data": Image(file="data:image/jpeg;base64," + encoded),
        "io": Image.fromIO(io.BytesIO(VALID_JPEG)),
        "bare": Image(file=encoded),
        "plain_path": Image(file=str(path)),
        "url_field": Image(file="", url=url),
    }[source]
    adapter, event, requests, uploads, posts, _ = await image_adapter(serve, tmp_path)
    try:
        await event.send(MessageChain([image]))
        assert len(uploads) == len(posts) == 1
        assert posts[0][1] == {"threadId": "peer", "kind": "image", "sourceId": "source-1"}
        assert adapter.state.message_status("m1") == "replied"
        assert adapter.state.send_record(posts[0][0])["send_id"] == "job-1"
        assert requests.index(("POST", "/v1/accounts/a/uploads")) < requests.index(
            ("POST", "/v1/accounts/a/sends")
        )
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("thread", ["peer", "room@chatroom"])
@pytest.mark.parametrize("proactive", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
async def test_multiple_images_and_mixed_order(serve, tmp_path, thread, proactive, mixed):
    adapter, event, _, uploads, posts, _ = await image_adapter(
        serve, tmp_path, thread=thread, outcome="poll"
    )
    parts = [Image.fromBytes(VALID_JPEG), Image.fromBytes(VALID_JPEG)]
    if mixed:
        parts = [Plain("before"), parts[0], Plain("between"), parts[1], Plain("after")]
    try:
        if proactive:
            session = SimpleNamespace(
                session_id=f"a|{'group' if thread.endswith('@chatroom') else 'private'}|{thread}",
                platform_id="native-a",
            )
            await adapter.send_by_session(session, MessageChain(parts))
        else:
            await event.send(MessageChain(parts))
        assert len(uploads) == 2
        assert [p.get("text", p.get("sourceId")) for _, p in posts] == (
            ["before", "source-1", "between", "source-2", "after"]
            if mixed
            else ["source-1", "source-2"]
        )
        assert all("mentions" not in payload for _, payload in posts)
    finally:
        await adapter.terminate()


async def test_quote_and_mentions_stay_on_first_text(serve, tmp_path):
    adapter, event, _, _, posts, _ = await image_adapter(serve, tmp_path, thread="room@chatroom")
    try:
        await event.send(
            MessageChain(
                [
                    Reply(id="m1"),
                    At(qq="user"),
                    Image.fromBytes(VALID_JPEG),
                    Plain("caption"),
                    Image.fromBytes(VALID_JPEG),
                    Plain("end"),
                ]
            )
        )
        assert posts[0][1]["kind"] == "image"
        assert posts[1][1]["quote"] == {"messageId": "m1", "serverId": "900"}
        assert posts[1][1]["mentions"] == {"userIds": ["user"]}
        assert "quote" not in posts[-1][1]
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("failed", "failed"),
        ("partial", "partial"),
        ("cancelled", "failed"),
        ("http400", "failed"),
        ("queued", "needs_review"),
        ("running", "needs_review"),
        ("unknown", "needs_review"),
        ("needs_review", "needs_review"),
        ("lost", "needs_review"),
        ("http408", "needs_review"),
        ("projection_unknown", "needs_review"),
        ("missing_receipt", "needs_review"),
        ("read503", "needs_review"),
    ],
)
async def test_upload_success_send_failure_blocks_resend(serve, tmp_path, outcome, expected):
    adapter, event, _, uploads, posts, _ = await image_adapter(serve, tmp_path, outcome=outcome)
    if outcome in {"queued", "running", "read503", "missing_receipt"}:
        adapter.client.send_timeout = 0.1
        adapter.client.video_send_timeout = 0.1
    try:
        with pytest.raises(Exception):
            await event.send(MessageChain([Image.fromBytes(VALID_JPEG), Plain("must not send")]))
        assert adapter.state.message_status("m1") == expected
        row = adapter.state.send_record(posts[0][0])
        assert row["state"] == ("cancelled" if outcome == "cancelled" else expected)
        if outcome not in {"http400", "lost", "http408"}:
            assert row["send_id"] == "job-1"
        with pytest.raises(RuntimeError, match="人工检查"):
            await event.send(MessageChain([Image.fromBytes(VALID_JPEG)]))
        assert len(uploads) == len(posts) == 1
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("fail_at", [1, 2])
async def test_earlier_success_retained_as_partial(serve, tmp_path, fail_at):
    adapter, event, _, uploads, posts, _ = await image_adapter(
        serve, tmp_path, outcome="failed", fail_at=fail_at
    )
    try:
        parts = [
            Plain("sent"),
            Image.fromBytes(VALID_JPEG),
            Image.fromBytes(VALID_JPEG),
            Plain("never"),
        ]
        with pytest.raises(module("native_client").SendError):
            await event.send(MessageChain(parts))
        assert adapter.state.message_status("m1") == "partial"
        assert len(uploads) == fail_at
        assert len(posts) == fail_at + 1
        assert adapter.state.send_record(posts[0][0])["state"] == "accepted"
    finally:
        await adapter.terminate()


async def test_5xx_submission_looks_up_original_job_without_repost(serve, tmp_path):
    adapter, event, requests, uploads, posts, _ = await image_adapter(
        serve, tmp_path, outcome="http503"
    )
    try:
        await event.send(MessageChain([Image.fromBytes(VALID_JPEG)]))
        assert len(uploads) == len(posts) == 1
        assert ("GET", "/v1/accounts/a/sends/by-idempotency-key") in requests
        assert adapter.state.message_status("m1") == "replied"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "bad",
    [b"not an image", b"<svg></svg>", b"BMfake", VALID_JPEG + b"x" * (10 * 1024 * 1024)],
    ids=["invalid", "svg", "bmp", "oversize"],
)
async def test_unsupported_or_oversized_never_uploaded(serve, tmp_path, bad):
    adapter, event, requests, uploads, posts, _ = await image_adapter(serve, tmp_path)
    try:
        with pytest.raises(ValueError):
            await event.send(MessageChain([Image.fromBytes(bad)]))
        assert not uploads and not posts
        assert adapter.state.message_status("m1") == "failed"
        assert not any(method == "POST" for method, _ in requests)
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a",
        "http://10.0.0.1/a",
        "http://169.254.169.254/a",
        "http://[::1]/a",
        "http://[::ffff:127.0.0.1]/a",
        "http://224.0.0.1/a",
        "ftp://public.example/a",
        "https://user:pass@public.example/a",
        "http://public.example:6174/a",
    ],
)
async def test_unsafe_url_rejected(url):
    with pytest.raises(ValueError):
        await module("native_client").load_outgoing_image(Image(file=url))


@pytest.mark.parametrize(
    "addresses", [["127.0.0.1"], ["10.1.1.1"], ["8.8.8.8", "::1"], ["169.254.169.254"]]
)
async def test_dns_answers_checked_at_connection(monkeypatch, addresses):
    native = module("native_client")
    resolver = native._PublicMediaResolver()

    async def resolve(*args):
        return [{"host": address} for address in addresses]

    monkeypatch.setattr(resolver.resolver, "resolve", resolve)
    try:
        with pytest.raises(ValueError, match="公网"):
            await resolver.resolve("public.example", 80)
    finally:
        await resolver.close()


@pytest.mark.parametrize(
    "kind",
    [
        "private_redirect",
        "redirect_loop",
        "oversize",
        "chunked_oversize",
        "format",
        "timeout",
        "compressed",
    ],
)
async def test_url_download_limits(public_image_url, kind):
    native = module("native_client")

    async def handler(req):
        if kind == "private_redirect":
            raise web.HTTPFound("http://127.0.0.1/private")
        if kind == "redirect_loop":
            raise web.HTTPFound("/again")
        if kind == "oversize":
            return web.Response(headers={"Content-Length": str(11 * 1024 * 1024)})
        if kind == "chunked_oversize":
            response = web.StreamResponse()
            await response.prepare(req)
            try:
                for _ in range(161):
                    await response.write(b"x" * 65536)
            except ConnectionError:
                pass
            return response
        if kind == "timeout":
            await asyncio.sleep(0.05)
        if kind == "compressed":
            return web.Response(body=b"fake", headers={"Content-Encoding": "gzip"})
        return web.Response(body=b"not image")

    url = await public_image_url(handler)
    with pytest.raises((ValueError, TimeoutError)):
        async with asyncio.timeout(0.02 if kind == "timeout" else 5):
            await native.load_outgoing_image(Image.fromURL(url))


async def test_redirect_to_public_image(public_image_url):
    async def handler(req):
        if req.path == "/image":
            raise web.HTTPFound("/final")
        return web.Response(body=VALID_JPEG)

    url = await public_image_url(handler)
    data, mime = await module("native_client").load_outgoing_image(Image.fromURL(url))
    assert data == VALID_JPEG and mime == "image/jpeg"


async def test_pending_job_id_survives_shutdown_and_restart(serve, tmp_path):
    adapter, event, _, uploads, posts, _ = await image_adapter(serve, tmp_path, outcome="running")
    adapter.client.send_timeout = 30
    task = asyncio.create_task(event.send(MessageChain([Image.fromBytes(VALID_JPEG)])))
    for _ in range(200):
        await asyncio.sleep(0.005)
        if posts and adapter.state.send_record(posts[0][0])["send_id"]:
            break
    assert posts and adapter.state.send_record(posts[0][0])["send_id"] == "job-1"
    path = next(tmp_path.glob("*.sqlite3"))
    await adapter.terminate()
    await asyncio.gather(task, return_exceptions=True)
    state = module("state").StateStore(path)
    try:
        assert state.send_record(posts[0][0])["send_id"] == "job-1"
        assert state.send_record(posts[0][0])["state"] == "needs_review"
        assert state.message_status("m1") == "needs_review"
        assert len(uploads) == len(posts) == 1
    finally:
        state.close()


async def test_recall_during_upload_prevents_send(serve, tmp_path, monkeypatch):
    adapter, event, _, uploads, posts, _ = await image_adapter(serve, tmp_path)
    request = adapter.client._request

    async def recall_after_upload(method, path, **kwargs):
        result = await request(method, path, **kwargs)
        if path.endswith("/uploads"):
            adapter.state.recall("peer", "900", "m1")
        return result

    monkeypatch.setattr(adapter.client, "_request", recall_after_upload)
    try:
        with pytest.raises(ValueError):
            await event.send(MessageChain([Image.fromBytes(VALID_JPEG)]))
        assert len(uploads) == 1 and not posts
        assert adapter.state.message_status("m1") == "recalled"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("upload400", "failed"),
        ("upload503", "needs_review"),
        ("upload_disconnect", "needs_review"),
        ("upload_timeout", "needs_review"),
    ],
)
async def test_upload_failure_never_submits_send_or_retries(
    serve, tmp_path, monkeypatch, outcome, expected
):
    adapter, event, requests, uploads, posts, _ = await image_adapter(
        serve, tmp_path, outcome=outcome
    )
    if outcome == "upload_timeout":
        request = adapter.client._request

        async def request_with_upload_timeout(method, path, **kwargs):
            if path.endswith("/uploads"):
                kwargs["timeout"] = aiohttp.ClientTimeout(total=0.03)
            return await request(method, path, **kwargs)

        monkeypatch.setattr(adapter.client, "_request", request_with_upload_timeout)
    try:
        with pytest.raises(module("native_client").NativeAPIError):
            await event.send(MessageChain([Image.fromBytes(VALID_JPEG), Plain("never")]))
        assert adapter.state.message_status("m1") == expected
        assert len(uploads) == 1 and not posts
        assert len([path for method, path in requests if method == "POST"]) == 1
        with pytest.raises(RuntimeError, match="人工检查"):
            await event.send(MessageChain([Image.fromBytes(VALID_JPEG)]))
        assert len(uploads) == 1
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "foreign-job"},
        {"accountId": "foreign"},
        {"threadId": "foreign"},
        {"kind": "download"},
        {"sourceId": "foreign"},
        {"serverMessageId": "changed"},
    ],
)
async def test_polled_job_identity_mismatch_is_retained(serve, tmp_path, monkeypatch, changes):
    adapter, event, _, uploads, posts, jobs = await image_adapter(serve, tmp_path, outcome="queued")

    async def bad_job(job_id):
        return {**jobs[job_id], **changes, "state": "accepted"}

    monkeypatch.setattr(adapter.client, "job", bad_job)
    try:
        with pytest.raises(module("native_client").SendError, match="receipt_identity_mismatch"):
            await event.send(MessageChain([Image.fromBytes(VALID_JPEG), Plain("never")]))
        assert len(uploads) == len(posts) == 1
        assert adapter.state.message_status("m1") == "needs_review"
        assert adapter.state.send_record(posts[0][0])["send_id"] == "job-1"
    finally:
        await adapter.terminate()


async def test_later_invalid_image_leaves_accepted_image_and_partial(serve, tmp_path):
    adapter, event, _, uploads, posts, _ = await image_adapter(serve, tmp_path)
    try:
        with pytest.raises(ValueError):
            await event.send(
                MessageChain([Image.fromBytes(VALID_JPEG), Image.fromBytes(b"invalid")])
            )
        assert len(uploads) == len(posts) == 1
        assert adapter.state.message_status("m1") == "partial"
        assert adapter.state.send_record(posts[0][0])["state"] == "accepted"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("metadata", [Reply(id="m1"), At(qq="user")])
async def test_image_only_explicit_quote_or_mention_rejected_before_upload(
    serve, tmp_path, metadata
):
    adapter, event, _, uploads, posts, _ = await image_adapter(
        serve, tmp_path, thread="room@chatroom"
    )
    try:
        with pytest.raises(ValueError, match="文本"):
            await event.send(MessageChain([metadata, Image.fromBytes(VALID_JPEG)]))
        assert not uploads and not posts
    finally:
        await adapter.terminate()


async def test_oversized_local_file_rejected(tmp_path):
    path = tmp_path / "large.jpg"
    with path.open("wb") as stream:
        stream.truncate(10 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="大小限制"):
        await module("native_client").load_outgoing_image(Image.fromFileSystem(path))
