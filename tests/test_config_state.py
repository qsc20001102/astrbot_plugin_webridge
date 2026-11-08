import pytest
from conftest import module


def test_config_token_file_and_url(tmp_path):
    config_module = module("config")
    Config = config_module.NativeConfig
    token = tmp_path / "secret"
    token.write_text("synthetic-test-token\n")
    cfg = Config.from_dict(
        {
            "server_url": "https://native.example/prefix/v1/",
            "account_id": "a",
            "token_file": str(token),
        }
    )
    assert cfg.api_url == "https://native.example/prefix/v1"
    assert cfg.load_token() == "synthetic-test-token"
    assert "synthetic-test-token" not in repr(cfg)
    assert set(config_module.DEFAULT_CONFIG) == {
        "id",
        "type",
        "enable",
        "account_id",
    }
    assert config_module.MAX_IMAGE_BYTES == 10 * 1024 * 1024
    assert config_module.MEDIA_DOWNLOAD_TIMEOUT == 60
    assert config_module.MAX_CONCURRENT_MEDIA_TASKS == 3


@pytest.mark.parametrize(
    "change",
    [
        {"server_url": "file:///tmp/api"},
        {"server_url": "https://user:pass@host"},
        {"server_url": "https://host/?token=abc"},
        {"account_id": ""},
        {"token": "bad\nheader"},
    ],
)
def test_invalid_config_rejected(change):
    data = {"server_url": "http://localhost:6174", "account_id": "a", "token": "fake"}
    data.update(change)
    with pytest.raises(ValueError):
        module("config").NativeConfig.from_dict(data).load_token()


def test_convert_message_attaches_image_without_a_prompt():
    from astrbot.api.message_components import At, Image, Reply

    config_module = module("config")
    event_module = module("event")
    cfg = config_module.NativeConfig.from_dict(
        {"server_url": "http://localhost:6174", "account_id": "a", "token": "fake"}
    )
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    direct = event_module.convert_message(
        {
            "id": "m1",
            "threadId": "peer",
            "senderId": "u1",
            "type": 3,
            "body": "",
        },
        cfg,
        "bot",
        image_bytes=png_bytes,
    )
    assert direct.message_str == ""
    assert [type(c) for c in direct.message] == [Image]

    quoted_group = event_module.convert_message(
        {
            "id": "m2",
            "threadId": "room@chatroom",
            "senderId": "u1",
            "type": 49,
            "appMessageType": 57,
            "body": "@机器人\u2005",
            "mentions": {"state": "known", "self": True},
            "quote": {
                "serverId": "888",
                "senderId": "author",
                "type": 3,
                "state": "embedded",
                "embeddedImage": True,
                "embeddedText": "",
            },
        },
        cfg,
        "bot",
        image_bytes=png_bytes,
    )
    assert quoted_group.message_str == ""
    assert [type(c) for c in quoted_group.message] == [At, Reply, Image]
    reply = quoted_group.message[1]
    assert reply.message_str == "[图片]"
    assert not any(isinstance(c, Image) for c in reply.chain)


def test_state_deduplicates_atomically_and_survives_restart(tmp_path):
    State = module("state").StateStore
    path = tmp_path / "state.sqlite3"
    state = State(path)
    assert state.cursor == "0"
    assert state.accept("1", "m1", "peer", "900", "queued")
    assert not state.accept("2", "m1", "peer", "900", "queued")
    assert state.cursor == "2"
    state.close()
    state = State(path)
    assert state.cursor == "2"
    assert state.message_status("m1") == "needs_review"
    state.recall("peer", "900", "m1")
    assert state.message_status("m1") == "recalled"
    assert not state.accept("3", "m1", "peer", "900", "queued")
    state.close()


def test_outbox_retains_key_without_content_or_token(tmp_path):
    State = module("state").StateStore
    state = State(tmp_path / "state.sqlite3")
    state.accept("1", "m1", "p", "9", "queued")
    state.begin_send("stable-key", "m1", "p")
    state.finish_send("stable-key", "needs_review", "send-1")
    assert state.send_record("stable-key")["state"] == "needs_review"
    state.close()
    state = State(tmp_path / "state.sqlite3")
    assert state.send_record("stable-key")["send_id"] == "send-1"
    state.close()


def test_original_lookup_requires_unique_message_in_same_chat(tmp_path):
    state = module("state").StateStore(tmp_path / "state.sqlite3")
    try:
        state.accept("1", "original", "peer", "899", "ignored")
        state.accept("2", "foreign-original", "another-chat", "899", "ignored")
        assert state.find_message_id("peer", "899") == "original"
        assert state.find_message_id("unknown-chat", "899") is None
        assert state.find_message_id("peer", "unknown-server") is None
        state.accept("3", "duplicate", "peer", "899", "ignored")
        assert state.find_message_id("peer", "899") is None
    finally:
        state.close()


def test_server_replacement_never_reuses_old_cursor(tmp_path):
    state_module = module("state")
    state = state_module.StateStore(tmp_path / "state.sqlite3")
    state.bind_server("native-server-1")
    state.advance("10000")
    with pytest.raises(state_module.StateIdentityError):
        state.bind_server("native-server-2")
    assert state.cursor == "10000"
    state.close()


def test_reused_local_message_id_is_explicitly_held_for_review(tmp_path):
    state_module = module("state")
    state = state_module.StateStore(tmp_path / "state.sqlite3")
    state.accept("1", "m1", "peer", "900", "queued")
    with pytest.raises(state_module.StateIdentityError):
        state.accept("2", "m1", "peer", "901", "queued")
    assert state.cursor == "1"
    assert state.message_status("m1") == "needs_review"
    state.close()


def test_recall_of_old_server_identity_does_not_recall_other_message(tmp_path):
    state = module("state").StateStore(tmp_path / "state.sqlite3")
    state.accept("1", "m1", "peer", "901", "queued")
    state.recall("peer", "900", "m1")
    assert state.message_status("m1") == "queued"
    state.close()


def test_recall_without_local_id_blocks_later_commit_across_restart(tmp_path):
    State = module("state").StateStore
    path = tmp_path / "state.sqlite3"
    state = State(path)
    state.recall("peer", "900", None)
    state.close()
    state = State(path)
    assert not state.accept("2", "late-local-id", "peer", "900", "queued")
    assert state.message_status("late-local-id") == "recalled"
    state.close()


@pytest.mark.parametrize(
    "initial,pending,recovered",
    [("replied", "replied", "partial"), ("no_reply", "queued", "needs_review")],
)
def test_pending_later_submission_is_visible_after_restart(tmp_path, initial, pending, recovered):
    State = module("state").StateStore
    path = tmp_path / "state.sqlite3"
    state = State(path)
    state.accept("1", "m1", "peer", "900", "queued")
    if initial == "replied":
        state.begin_send("first-key", "m1", "peer")
        state.finish_send("first-key", "accepted", "s1")
    state.mark_message("m1", initial)
    state.begin_send("pending-key", "m1", "peer")
    try:
        assert state.message_status("m1") == pending
    finally:
        state.close()  # Abrupt process loss: no adapter cancellation handler runs.
    state = State(path)
    try:
        assert state.message_status("m1") == recovered
        assert state.send_record("pending-key")["state"] == "needs_review"
        if initial == "replied":
            assert state.send_record("first-key")["state"] == "accepted"
    finally:
        state.close()


@pytest.mark.parametrize("initial", ["failed", "needs_review", "partial"])
def test_late_accepted_receipt_preserves_failure_gate(tmp_path, initial):
    state = module("state").StateStore(tmp_path / "state.sqlite3")
    try:
        state.accept("1", "m1", "peer", "900", "queued")
        state.mark_message("m1", initial)
        state.mark_message("m1", "replied")
        assert state.message_status("m1") == "partial"
    finally:
        state.close()
