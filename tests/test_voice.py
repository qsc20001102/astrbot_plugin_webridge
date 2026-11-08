"""Native v1.4.0 voice jobs, decoded WAV delivery and AstrBot ASR integration."""

import asyncio
import base64
import hashlib
import io
import sqlite3
import struct
import wave
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Image, Plain, Record, Reply
from conftest import module
from test_adapter import make_adapter, message, native_event, no_telemetry  # noqa: F401
from test_native_client import VALID_JPEG


def wav(seconds=0.1, *, rate=16000, channels=1):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\x00\x01" * int(seconds * rate) * channels)
    return stream.getvalue()


WAV = wav()


def float_wav():
    data = b"\0" * (1600 * 4)
    fmt = struct.pack("<HHIIHH", 3, 1, 16000, 64000, 4, 32)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )


def chunks(states=("accepted",), duration=100):
    return [
        {
            "index": index,
            "state": state,
            "durationMs": duration,
            "serverMessageId": str(910 + index) if state == "accepted" else None,
        }
        for index, state in enumerate(states)
    ]


class VoiceEnv:
    def __init__(self, trigger):
        self.messages = {trigger["id"]: trigger}
        self.assets, self.contents, self.jobs = {}, {}, {}
        self.requests, self.posts, self.uploads = [], [], []
        self.outcome = "accepted"
        self.send_chunks = chunks()
        self.hooks = {}
        self.adapter = None
        self.media_status = None

    def add_voice(self, msg, *, data=WAV, state="available", **changes):
        self.messages[msg["id"]] = msg
        asset = {
            "id": "voice-" + msg["id"],
            "accountId": "a",
            "messageId": msg["id"],
            "kind": "voice",
            "variant": "normal",
            "actualVariant": "normal",
            "state": state,
            "contentType": "audio/wav",
            "byteLength": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            **changes,
        }
        self.assets[msg["id"]] = [asset]
        self.contents[asset["id"]] = data
        return asset

    async def handle(self, req, fallback):
        self.requests.append((req.method, req.path))
        path = req.path
        if req.method == "GET" and path.endswith("/media"):
            if self.media_status:
                return web.json_response(
                    {"error": {"code": "insufficient_scope"}}, status=self.media_status
                )
            return web.json_response({"items": self.assets.get(path.split("/")[-2], [])})
        if req.method == "GET" and path.endswith("/content"):
            if "content" in self.hooks:
                await self.hooks["content"]()
            return web.Response(body=self.contents[path.split("/")[-2]], content_type="audio/wav")
        if req.method == "GET" and "/messages/" in path:
            return web.json_response(self.messages[path.rsplit("/", 1)[-1]])
        if req.method == "POST" and path.endswith("/media-transfers"):
            msg_id = path.split("/")[-2]
            asset = self.assets[msg_id][0]
            assert await req.json() == {
                "messageId": msg_id,
                "quality": "normal",
                "assetId": asset["id"],
            }
            job = {
                "id": "download-1",
                "kind": "download",
                "accountId": "a",
                "state": "queued",
                "assetId": asset["id"],
            }
            self.jobs[job["id"]] = job
            return web.json_response(job, status=202)
        if req.method == "POST" and path.endswith("/uploads"):
            form = await req.multipart()
            field = await form.next()
            assert field.name == "file"
            data = bytes(await field.read())
            source = {
                "id": f"src-{len(self.uploads)}",
                "byteLength": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            self.uploads.append((field.filename, field.headers["Content-Type"], data))
            if "upload" in self.hooks:
                await self.hooks["upload"]()
            return web.json_response(source, status=201)
        if req.method == "POST" and path.endswith("/sends"):
            payload = await req.json()
            key = req.headers["Idempotency-Key"]
            assert key not in [item[0] for item in self.posts]
            self.posts.append((key, payload))
            if "text" in payload:
                return web.json_response(
                    {
                        "id": f"text-{len(self.posts)}",
                        "accountId": "a",
                        "destination": payload["threadId"],
                        "state": "accepted",
                    }
                )
            assert set(payload) == {"threadId", "sourceId", "kind"}
            assert payload["kind"] in {"voice", "image"}
            parts = self.send_chunks if payload["kind"] == "voice" else []
            state = "queued" if self.outcome in {"poll", "lost", "http503"} else self.outcome
            job = {
                "id": f"send-{len(self.posts)}",
                "accountId": "a",
                "kind": "send",
                "threadId": payload["threadId"],
                "sourceId": payload["sourceId"],
                "state": state,
                "serverMessageId": (parts[-1]["serverMessageId"] if parts else "911"),
                "result": {
                    "messageIds": [
                        f"sent-{i}" for i, part in enumerate(parts) if part["state"] == "accepted"
                    ]
                    if parts
                    else ["sent-image"],
                    "profileProjection": "complete",
                    **({"chunks": parts} if payload["kind"] == "voice" else {}),
                },
            }
            self.jobs[job["id"]] = job
            if self.outcome in {"lost", "http503"}:
                return web.json_response({}, status=503)
            if "submitted" in self.hooks:
                await self.hooks["submitted"](job)
            return web.json_response(job, status=202)
        if req.method == "GET" and path.endswith("/sends/by-idempotency-key"):
            assert req.query["key"] == self.posts[-1][0]
            if self.outcome == "lost":
                return web.json_response({}, status=404)
            return web.json_response(list(self.jobs.values())[-1])
        if req.method == "GET" and "/jobs/" in path:
            job = self.jobs[path.rsplit("/", 1)[-1]]
            if "poll" in self.hooks:
                return await self.hooks["poll"](job)
            if job["kind"] == "download":
                for assets in self.assets.values():
                    for asset in assets:
                        if asset["id"] == job["assetId"]:
                            asset["state"] = "available"
                return web.json_response({**job, "state": "completed"})
            if self.outcome in {"poll", "http503"}:
                return web.json_response({**job, "state": "accepted"})
            return web.json_response(job)
        return await fallback(req)


async def env(serve, tmp_path, trigger=None, **options):
    trigger = trigger or message(type=34, body=None)
    result = VoiceEnv(trigger)

    async def custom_serve(fallback):
        async def handler(req):
            return await result.handle(req, fallback)

        return await serve(handler)

    adapter, queue, _, _ = await make_adapter(custom_serve, tmp_path, trigger, **options)
    result.adapter, result.queue = adapter, queue
    adapter.client.poll_interval = 0.001
    adapter.client.voice_send_timeout = 1
    return result


async def drain_voice(result, msg, cursor="1"):
    await result.adapter.process_native_event(native_event(msg, cursor))
    await asyncio.gather(*list(result.adapter._media_tasks))


@pytest.mark.parametrize(
    "source", ["path", "plain_path", "base64", "data", "url", "url_fallback", "path_fallback"]
)
@pytest.mark.parametrize("thread", ["peer", "room@chatroom"])
async def test_send_record_sources(serve, tmp_path, public_image_url, source, thread):
    result = await env(serve, tmp_path, message(threadId=thread))
    path = tmp_path / "TTS 语音.wav"
    path.write_bytes(WAV)
    encoded = base64.b64encode(WAV).decode()

    async def download(req):
        assert "Authorization" not in req.headers and "Cookie" not in req.headers
        return web.Response(body=WAV)

    url = await public_image_url(download)
    record = {
        "path": Record.fromFileSystem(path),
        "plain_path": Record(file=str(path)),
        "base64": Record.fromBase64(encoded),
        "data": Record(file="data:audio/wav;base64," + encoded),
        "url": Record.fromURL(url),
        "url_fallback": Record(file="tts.wav", url=url),
        "path_fallback": Record(file="tts.wav", path=str(path)),
    }[source]
    try:
        session = SimpleNamespace(
            session_id=f"a|{'group' if thread.endswith('@chatroom') else 'private'}|{thread}",
            platform_id="native-a",
        )
        await result.adapter.send_by_session(session, MessageChain([record]))
        assert result.uploads == [("voice.wav", "audio/wav", WAV)]
        key, payload = result.posts[0]
        assert payload == {"threadId": thread, "kind": "voice", "sourceId": "src-0"}
        assert result.adapter.state.send_record(key)["state"] == "accepted"
        assert result.adapter.state.voice_chunks(key)[0]["server_id"] == "910"
    finally:
        await result.adapter.terminate()


@pytest.mark.parametrize("seconds,notes", [(60, 1), (61, 2), (121, 3), (300, 5)])
async def test_long_voice_is_one_native_job_with_multiple_receipts(serve, tmp_path, seconds, notes):
    result = await env(serve, tmp_path)
    result.send_chunks = chunks(("accepted",) * notes, duration=seconds * 1000 // notes)
    try:
        await result.adapter.send_chain(
            "peer", MessageChain([Record.fromBase64(base64.b64encode(wav(seconds)).decode())])
        )
        assert len(result.uploads) == len(result.posts) == 1
        saved = result.adapter.state.voice_chunks(result.posts[0][0])
        assert [part["server_id"] for part in saved] == [str(910 + i) for i in range(notes)]
        assert all(part["state"] == "accepted" for part in saved)
    finally:
        await result.adapter.terminate()


async def test_text_image_voice_order_and_tts_metadata(serve, tmp_path):
    result = await env(serve, tmp_path, message(threadId="room@chatroom"))
    adapter = result.adapter
    adapter.state.accept("1", "m1", "room@chatroom", "900", "queued")
    event = module("event").WeBridgeEvent(
        module("event").convert_message(result.messages["m1"], adapter.native_config, "bot"),
        adapter,
    )
    try:
        await event.send(
            MessageChain(
                [
                    At(qq="user"),
                    Reply(id="m1"),
                    Plain("first"),
                    Record.fromBase64(base64.b64encode(WAV).decode(), text="TTS source"),
                    Image.fromBytes(VALID_JPEG),
                    Plain("last"),
                ]
            )
        )
        assert [payload.get("kind", "text") for _, payload in result.posts] == [
            "text",
            "voice",
            "image",
            "text",
        ]
        assert result.posts[0][1]["mentions"] == {"userIds": ["user"]}
        assert result.posts[0][1]["quote"] == {"messageId": "m1", "serverId": "900"}
        assert "TTS source" not in str([payload for _, payload in result.posts])
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "state,parts,expected,send_state",
    [
        ("failed", ("rejected",), "failed", "failed"),
        ("failed", ("accepted", "rejected"), "partial", "partial"),
        ("needs_review", ("accepted", "needs_review"), "partial", "needs_review"),
        ("running", ("accepted", "submitting"), "partial", "needs_review"),
        ("unknown", ("prepared",), "needs_review", "needs_review"),
        ("lost", ("prepared",), "needs_review", "needs_review"),
        ("queued", ("prepared",), "needs_review", "needs_review"),
        ("partial", ("accepted", "rejected"), "partial", "partial"),
    ],
)
async def test_voice_failure_retains_job_parts_and_never_resends(
    serve, tmp_path, state, parts, expected, send_state
):
    result = await env(serve, tmp_path, message())
    result.outcome, result.send_chunks = state, chunks(parts)
    adapter = result.adapter
    adapter.client.voice_send_timeout = 0.1
    adapter.state.accept("1", "m1", "peer", "900", "queued")
    event = module("event").WeBridgeEvent(
        module("event").convert_message(result.messages["m1"], adapter.native_config, "bot"),
        adapter,
    )
    try:
        with pytest.raises(module("native_client").SendError):
            await event.send(
                MessageChain([Record.fromBase64(base64.b64encode(WAV).decode()), Plain("never")])
            )
        key = result.posts[0][0]
        assert adapter.state.message_status("m1") == expected
        assert adapter.state.send_record(key)["state"] == send_state
        if state != "lost":
            assert adapter.state.send_record(key)["send_id"] == "send-1"
            assert len(adapter.state.voice_chunks(key)) == len(parts)
        with pytest.raises(RuntimeError, match="人工检查"):
            await event.send(MessageChain([Record.fromBase64(base64.b64encode(WAV).decode())]))
        assert len(result.posts) == len(result.uploads) == 1
    finally:
        await adapter.terminate()


async def test_voice_5xx_submission_reads_same_key_and_job(serve, tmp_path):
    result = await env(serve, tmp_path)
    result.outcome = "http503"
    try:
        await result.adapter.send_chain(
            "peer", MessageChain([Record.fromBase64(base64.b64encode(WAV).decode())])
        )
        assert len(result.posts) == len(result.uploads) == 1
        assert ("GET", "/v1/accounts/a/sends/by-idempotency-key") in result.requests
        assert ("GET", "/v1/accounts/a/jobs/send-1") in result.requests
    finally:
        await result.adapter.terminate()


@pytest.mark.parametrize("state", ["available", "pending"])
async def test_inbound_voice_native_wav_delivered_and_deduplicated(serve, tmp_path, state):
    msg = message(type=34, body=None)
    result = await env(serve, tmp_path, msg)
    result.add_voice(msg, state=state)
    try:
        await drain_voice(result, msg)
        event = result.queue.get_nowait()
        assert event.message_str == ""
        records = [part for part in event.get_messages() if isinstance(part, Record)]
        assert len(records) == 1
        assert records[0].file == "base64://" + base64.b64encode(WAV).decode()
        assert not any(isinstance(part, Plain) and part.text == "" for part in event.get_messages())
        if state == "pending":
            assert (
                len(
                    [
                        path
                        for method, path in result.requests
                        if method == "POST" and path.endswith("/media-transfers")
                    ]
                )
                == 1
            )
        await drain_voice(result, msg, "2")
        assert result.queue.empty()
    finally:
        await result.adapter.terminate()


async def test_native_wav_reaches_actual_astrbot_asr_stage(serve, tmp_path):
    from astrbot.core.pipeline.preprocess_stage.stage import PreProcessStage

    msg = message(type=34, body=None)
    result = await env(serve, tmp_path, msg)
    result.add_voice(msg)
    origins, audio_paths = [], []

    class ASR:
        async def get_text(self, audio_url):
            audio_paths.append(audio_url)
            assert await asyncio.to_thread(Path(audio_url).read_bytes) == WAV
            return "今天有什么安排"

    provider = ASR()

    async def using_provider(origin):
        origins.append(origin)
        return provider

    context = SimpleNamespace(
        get_using_stt_provider_async=using_provider, get_using_stt_provider=lambda origin: provider
    )
    try:
        await drain_voice(result, msg)
        event = result.queue.get_nowait()
        stage = PreProcessStage()
        stage.config, stage.stt_settings, stage.platform_settings = {}, {"enable": True}, {}
        stage.plugin_manager = SimpleNamespace(context=context)
        await stage.process(event)
        assert audio_paths
        assert event.message_str == "今天有什么安排"
        assert any(
            isinstance(part, Plain) and part.text == "今天有什么安排"
            for part in event.get_messages()
        )
        await event.send(MessageChain([Plain("收到你的语音")]))
        assert result.posts[-1][1]["text"] == "收到你的语音"
    finally:
        await result.adapter.terminate()


async def test_missing_quoted_voice_preserves_text_without_replying(serve, tmp_path):
    request = message(
        id="request",
        threadId="room@chatroom",
        body="@机器人\u2005帮我听听",
        type=49,
        appMessageType=57,
        quote={"serverId": "nonexistent", "senderId": "user", "type": 34, "state": "unsupported"},
        mentions={"state": "known", "self": True},
    )
    result = await env(serve, tmp_path, request)
    try:
        await drain_voice(result, request)
        assert result.queue.get_nowait().message_str == "帮我听听"
        assert result.posts == []
    finally:
        await result.adapter.terminate()


@pytest.mark.parametrize(
    "bad", ["integrity", "foreign", "silk", "size", "duration", "scope", "timeout", "float"]
)
async def test_bad_inbound_voice_never_reaches_asr(serve, tmp_path, monkeypatch, bad):
    msg = message(type=34, body=None)
    result = await env(serve, tmp_path, msg)
    asset = result.add_voice(msg)
    if bad == "integrity":
        asset["sha256"] = "f" * 64
    elif bad == "foreign":
        asset["accountId"] = "foreign"
    elif bad == "silk":
        result.contents[asset["id"]] = b"#!SILK_V3bad"
        asset.update(
            byteLength=len(result.contents[asset["id"]]),
            sha256=hashlib.sha256(result.contents[asset["id"]]).hexdigest(),
        )
    elif bad == "size":
        asset["byteLength"] = module("config").MAX_VOICE_BYTES + 1
    elif bad == "duration":
        result.add_voice(msg, data=wav(61))
    elif bad == "scope":
        result.media_status = 403
    elif bad == "float":
        result.add_voice(msg, data=float_wav())
    else:
        result.adapter.client.media_timeout = 0.02

        async def slow():
            await asyncio.sleep(0.06)

        result.hooks["content"] = slow
    try:
        await drain_voice(result, msg)
        assert result.queue.empty()
        assert result.posts == []
    finally:
        await result.adapter.terminate()


async def test_voice_recall_during_download_suppresses_asr_and_reply(serve, tmp_path):
    msg = message(type=34, body=None)
    result = await env(serve, tmp_path, msg)
    result.add_voice(msg)

    async def recall():
        await result.adapter.process_native_event(
            {
                "cursor": "2",
                "kind": "message.recalled",
                "data": {"threadId": "peer", "serverId": "900", "messageId": "m1"},
            }
        )

    result.hooks["content"] = recall
    try:
        await drain_voice(result, msg)
        assert result.queue.empty() and not result.posts
        assert result.adapter.state.message_status("m1") == "recalled"
    finally:
        await result.adapter.terminate()


def test_streaming_wav_placeholder_sizes_are_normalized():
    client = module("native_client")
    data = bytearray(WAV)
    data[4:8] = (0xFFFFFFE4).to_bytes(4, "little")
    data[40:44] = (0xFFFFFFE0).to_bytes(4, "little")
    streaming = bytes(data)
    assert client.validate_voice_bytes(streaming) == "audio/wav"
    fixed = client._normalize_streaming_wav(streaming)
    assert int.from_bytes(fixed[4:8], "little") == len(streaming) - 8
    assert int.from_bytes(fixed[40:44], "little") == len(streaming) - 44
    assert fixed[44:] == streaming[44:]
    assert client._normalize_streaming_wav(fixed) == fixed


@pytest.mark.parametrize("bad", ["unknown", "empty_pcm", "too_long", "too_large"])
async def test_bad_outgoing_audio_is_not_uploaded(serve, tmp_path, bad):
    result = await env(serve, tmp_path)
    data = {
        "unknown": b"not audio",
        "empty_pcm": WAV[:44],
        "too_long": wav(301),
        "too_large": b"ID3" + b"x" * module("config").MAX_VOICE_BYTES,
    }[bad]
    try:
        with pytest.raises(ValueError):
            await result.adapter.send_chain(
                "peer", MessageChain([Record.fromBase64(base64.b64encode(data).decode())])
            )
        assert not result.uploads and not result.posts
    finally:
        await result.adapter.terminate()


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/a", "http://169.254.169.254/a", "http://[::1]/a", "ftp://public.example/a"],
)
async def test_voice_url_uses_same_ssrf_protection(url):
    with pytest.raises(ValueError):
        await module("native_client").load_outgoing_voice(Record(file=url))


def test_pending_voice_part_receipts_survive_restart(tmp_path):
    Store = module("state").StateStore
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    store.accept("1", "m1", "peer", "900", "queued")
    store.begin_send("key", "m1", "peer")
    store.record_send_id("key", "job-1")
    store.record_voice_chunks("key", chunks(("accepted", "submitting")))
    store.close()
    store = Store(path)
    try:
        assert store.message_status("m1") == "partial"
        assert store.send_record("key")["state"] == "needs_review"
        assert store.send_record("key")["send_id"] == "job-1"
        assert store.voice_chunks("key")[0]["server_id"] == "910"
    finally:
        store.close()


def test_voice_schema_addition_preserves_old_image_outbox(tmp_path):
    path = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE sends (key TEXT PRIMARY KEY, message_id TEXT, "
            "thread TEXT NOT NULL, state TEXT NOT NULL, send_id TEXT)"
        )
        db.execute("INSERT INTO sends VALUES ('image-key', NULL, 'peer', 'accepted', 'image-job')")
    store = module("state").StateStore(path)
    try:
        assert store.send_record("image-key")["send_id"] == "image-job"
        assert store.send_record("image-key")["state"] == "accepted"
        assert store.voice_chunks("image-key") == []
    finally:
        store.close()


async def test_voice_job_progress_preserves_each_note_id(serve, tmp_path):
    result = await env(serve, tmp_path)
    result.outcome = "running"
    result.send_chunks = chunks(("prepared", "prepared"))
    count = 0

    async def progress(job):
        nonlocal count
        count += 1
        accepted = count > 1
        parts = chunks(("accepted", "accepted" if accepted else "submitting"))
        return web.json_response(
            {
                **job,
                "state": "accepted" if accepted else "running",
                "serverMessageId": "911" if accepted else None,
                "result": {
                    "chunks": parts,
                    "messageIds": ["m10", "m11"] if accepted else ["m10"],
                    "profileProjection": "complete",
                },
            }
        )

    result.hooks["poll"] = progress
    try:
        await result.adapter.send_chain(
            "peer", MessageChain([Record.fromBase64(base64.b64encode(WAV).decode())])
        )
        assert count == 2
        assert len(result.posts) == len(result.uploads) == 1
        assert [
            part["server_id"] for part in result.adapter.state.voice_chunks(result.posts[0][0])
        ] == ["910", "911"]
    finally:
        await result.adapter.terminate()


async def test_voice_shutdown_after_first_note_keeps_partial_receipts(serve, tmp_path):
    msg = message()
    result = await env(serve, tmp_path, msg)
    result.outcome, result.send_chunks = "running", chunks(("accepted", "submitting"))
    adapter = result.adapter
    adapter.client.voice_send_timeout = 30
    adapter.state.accept("1", "m1", "peer", "900", "queued")
    event = module("event").WeBridgeEvent(
        module("event").convert_message(msg, adapter.native_config, "bot"), adapter
    )
    task = asyncio.create_task(
        event.send(MessageChain([Record.fromBase64(base64.b64encode(WAV).decode())]))
    )
    try:
        for _ in range(200):
            await asyncio.sleep(0.005)
            if result.posts and adapter.state.voice_chunks(result.posts[0][0]):
                break
        assert result.posts and adapter.state.voice_chunks(result.posts[0][0])
        path = next(tmp_path.glob("*.sqlite3"))
    finally:
        await adapter.terminate()
        await asyncio.gather(task, return_exceptions=True)
    store = module("state").StateStore(path)
    try:
        assert store.message_status("m1") == "partial"
        assert store.send_record(result.posts[0][0])["state"] == "needs_review"
        assert store.voice_chunks(result.posts[0][0])[0]["server_id"] == "910"
        assert len(result.posts) == len(result.uploads) == 1
    finally:
        store.close()


@pytest.mark.parametrize(
    "changes", [{"serverMessageId": "changed"}, {"state": "prepared"}, {"durationMs": 101}]
)
async def test_voice_changed_part_receipt_cannot_replace_accepted_identity(
    serve, tmp_path, changes
):
    result = await env(serve, tmp_path)
    result.outcome, result.send_chunks = "running", chunks(("accepted", "submitting"))

    async def changed(job):
        altered = chunks(("accepted", "submitting"))
        altered[0].update(changes)
        return web.json_response({**job, "result": {"chunks": altered}})

    result.hooks["poll"] = changed
    try:
        with pytest.raises(module("state").StateIdentityError):
            await result.adapter.send_chain(
                "peer", MessageChain([Record.fromBase64(base64.b64encode(WAV).decode())])
            )
        saved = result.adapter.state.voice_chunks(result.posts[0][0])
        assert saved[0]["server_id"] == "910" and saved[0]["state"] == "accepted"
        assert saved[0]["duration_ms"] == 100
        assert result.adapter.state.send_record(result.posts[0][0])["state"] == "needs_review"
        assert len(result.posts) == 1
    finally:
        await result.adapter.terminate()


async def test_already_running_voice_download_reuses_job_and_normal_variant(serve, tmp_path):
    msg = message(type=34, body=None)
    result = await env(serve, tmp_path, msg)
    normal = result.add_voice(msg, state="pending", jobId="download-1")
    encoded = {
        **normal,
        "id": "encoded-1",
        "variant": "encoded",
        "actualVariant": "encoded",
        "state": "available",
        "jobId": None,
    }
    result.assets["m1"].insert(0, encoded)
    result.jobs["download-1"] = {
        "id": "download-1",
        "kind": "download",
        "accountId": "a",
        "state": "running",
        "assetId": normal["id"],
    }
    try:
        await drain_voice(result, msg)
        assert result.queue.qsize() == 1
        assert not any(path.endswith("/media-transfers") for _, path in result.requests)
        assert not any("encoded-1/content" in path for _, path in result.requests)
    finally:
        await result.adapter.terminate()


@pytest.mark.parametrize(
    "prefix,mime",
    [
        (b"ID3", "audio/mpeg"),
        (b"\xff\xfa\x90", "audio/mpeg"),
        (b"fLaC", "audio/flac"),
        (b"OggS", "audio/ogg"),
        (b"\xff\xf1\x00", "audio/aac"),
        (b"\x00\x00\x00\x18ftypM4A ", "audio/mp4"),
        (b"\x1aE\xdf\xa3", "audio/webm"),
        (b"\x02#!SILK_V3", "audio/silk"),
    ],
)
async def test_compressed_tts_source_is_passed_unchanged_for_native_decode(
    serve, tmp_path, prefix, mime
):
    result = await env(serve, tmp_path)
    data = prefix + b"synthetic audio payload"
    try:
        await result.adapter.send_chain(
            "peer", MessageChain([Record.fromBase64(base64.b64encode(data).decode())])
        )
        assert result.uploads[0][1:] == (mime, data)
        assert result.posts[0][1]["kind"] == "voice"
    finally:
        await result.adapter.terminate()


async def test_float_wav_tts_is_decoded_by_native_instead_of_rejected_locally(serve, tmp_path):
    result = await env(serve, tmp_path)
    data = float_wav()
    try:
        await result.adapter.send_chain(
            "peer", MessageChain([Record.fromBase64(base64.b64encode(data).decode())])
        )
        assert result.uploads[0][1:] == ("audio/wav", data)
        assert result.posts[0][1]["kind"] == "voice"
    finally:
        await result.adapter.terminate()


@pytest.mark.parametrize(
    "riff_size,data_size",
    [
        (0xFFFFFFE4, None),
        (len(WAV) + 100, None),
        (None, 0xFFFFFFFF),
        (0xFFFFFFE4, 0xFFFFFFFF),
    ],
)
def test_streaming_wav_uses_actual_pcm_frames(riff_size, data_size):
    data = bytearray(WAV)
    if riff_size is not None:
        data[4:8] = struct.pack("<I", riff_size)
    if data_size is not None:
        data[40:44] = struct.pack("<I", data_size)
    native = module("native_client")
    assert native.validate_voice_bytes(bytes(data)) == "audio/wav"
    with pytest.raises(native.MediaError):
        native.validate_voice_bytes(bytes(data), normal=True)


def test_streaming_wav_over_300_actual_seconds_is_rejected():
    data = bytearray(wav(301))
    data[4:8] = struct.pack("<I", 0xFFFFFFE4)
    data[40:44] = struct.pack("<I", 0xFFFFFFFF)
    native = module("native_client")
    with pytest.raises(native.MediaError) as error:
        native.validate_voice_bytes(bytes(data))
    assert error.value.reason == "too_large"


def test_riff_garbage_without_audio_format_is_rejected():
    native = module("native_client")
    with pytest.raises(native.MediaError):
        native.validate_voice_bytes(b"RIFF" + struct.pack("<I", 16) + b"WAVEdata" + b"\0" * 8)
