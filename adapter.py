"""微信消息适配器. AstrBot owns all message and reply policies."""

import asyncio
import hashlib
import random
import uuid
import weakref
from pathlib import Path

from astrbot.api import logger
from astrbot.api.message_components import Nodes, Record, Video
from astrbot.api.platform import (
    AstrBotMessage,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.api.star import StarTools

from .config import (
    ADAPTER_ID,
    CONFIG_METADATA,
    DEFAULT_CONFIG,
    MAX_CONCURRENT_MEDIA_TASKS,
    PLUGIN_DISPLAY_NAME,
    PLUGIN_ID,
    NativeConfig,
)
from .event import WeBridgeEvent, convert_message, serialize_chain, session_target
from .native_client import (
    MediaError,
    NativeAPIError,
    NativeClient,
    QuotedMediaMetadataError,
    SendError,
    load_outgoing_image,
    load_outgoing_video,
    load_outgoing_voice,
)
from .runtime import RuntimeUnavailable, get_runtime
from .state import StateStore, cursor_number


@register_platform_adapter(
    ADAPTER_ID,
    "微信消息收发；唤醒、权限和回复行为遵循 AstrBot 配置文件",
    default_config_tmpl=DEFAULT_CONFIG.copy(),
    adapter_display_name=PLUGIN_DISPLAY_NAME,
    logo_path="logo.png",
    support_streaming_message=False,
    config_metadata=CONFIG_METADATA,
)
class WeBridgeAdapter(Platform):
    reconnect_delay = 1.0

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
        *,
        state_dir: Path | None = None,
    ):
        super().__init__(platform_config, event_queue)
        self.managed = not platform_config.get("server_url")
        if self.managed:
            account = platform_config.get("account_id", "")
            if not isinstance(account, str):
                raise ValueError("微信账号必须是字符串")
            self.native_config = NativeConfig("", account.strip(), "", "")
        else:
            # Existing externally connected bots can be stopped/replaced without
            # adopting their credentials into the plugin-owned engine.
            self.native_config = NativeConfig.from_dict(platform_config)
        cfg = self.native_config
        self.client = NativeClient(
            cfg.api_url, cfg.account_id, cfg.token if self.managed else cfg.load_token()
        )
        # PlatformManager owns client_self_id (its instance/task UUID).
        self.bot_self_id = cfg.account_id
        root = state_dir if state_dir is not None else StarTools.get_data_dir(PLUGIN_ID)
        self._state_root = Path(root)
        identity = f"{cfg.api_url}\n{cfg.account_id}\n{platform_config.get('id', ADAPTER_ID)}"
        self.state = (
            None
            if self.managed
            else StateStore(
                Path(root) / (hashlib.sha256(identity.encode()).hexdigest() + ".sqlite3")
            )
        )
        self.stopped = False
        self._state_closed = False
        self._run_task = None
        self._send_tasks = set()
        self._media_tasks = set()
        self._media_semaphore = asyncio.Semaphore(MAX_CONCURRENT_MEDIA_TASKS)
        self._events = weakref.WeakValueDictionary()
        self._send_lock = asyncio.Lock()
        self._terminate_lock = asyncio.Lock()
        self._receive_locks = [asyncio.Lock() for _ in range(256)]

    async def _connect_managed(self):
        cfg = await get_runtime().connection(self.native_config.account_id)
        if self.native_config.account_id and cfg.account_id != self.native_config.account_id:
            raise RuntimeUnavailable("微信账号绑定发生变化，请重新选择账号")
        if self.state is None:
            identity = f"managed\n{cfg.account_id}\n{self.config.get('id', ADAPTER_ID)}"
            self.state = StateStore(
                self._state_root / (hashlib.sha256(identity.encode()).hexdigest() + ".sqlite3")
            )
        if cfg != self.native_config:
            await self.client.close()
            self.native_config = cfg
            self.client = NativeClient(cfg.api_url, cfg.account_id, cfg.token)
        await self.client.open()

    def _classify(self, raw: dict, origin: str) -> str:
        # Only transport filtering: replay/imports, echoes, recalls and unsupported
        # payloads. Never examine wake words, allowlists or a reply mode here.
        if (
            origin != "live"
            or raw.get("direction") != "incoming"
            or raw.get("recall") is not None
            or not isinstance(raw.get("senderId"), str)
            or not raw["senderId"]
            or raw["senderId"] == self.bot_self_id
        ):
            return "ignore"
        msg_type = raw.get("type")
        if type(msg_type) is not int:
            return "ignore"
        # Incoming videos are deliberately ignored, including in group chats.
        # Do not schedule a media task or hand an event to AstrBot.
        if msg_type in {3, 34}:
            return "media"
        if msg_type == 49 and raw.get("appMessageType") == 57:
            return "text" if isinstance(raw.get("body"), str) or raw.get("quote") else "ignore"
        if msg_type == 49 and raw.get("appMessageType") == 19:
            return "text"
        if msg_type == 1 and isinstance(raw.get("body"), str) and raw["body"].strip():
            return "text"
        return "ignore"

    @staticmethod
    def _media_kind(raw: dict) -> str | None:
        if type(raw.get("type")) is not int:
            return None
        kind = {3: "image", 34: "voice"}.get(raw.get("type"))
        if kind:
            return kind
        if raw.get("type") != 49:
            return None
        quote = raw.get("quote")
        if raw.get("appMessageType") == 57 and isinstance(quote, dict):
            if type(quote.get("type")) is not int:
                return None
            return {3: "image", 34: "voice"}.get(quote.get("type"))
        return None

    def _queue_event(self, event: WeBridgeEvent) -> None:
        message_id = event.message_obj.message_id
        try:
            self.commit_event(event)
        except Exception:
            self.state.mark_message(message_id, "needs_review")
            event.cleanup_temporary_local_files()
            raise RuntimeError("Native event handoff requires inspection") from None
        self._events[message_id] = event

    async def process_native_event(self, event: dict):
        if event.get("kind") == "message.recalled":
            # Recalls must be able to invalidate messages during media I/O.
            await self._process_native_event(event)
            return
        thread = event.get("data", {}).get("threadId", "")
        digest = hashlib.sha256(thread.encode() if isinstance(thread, str) else b"").digest()
        async with self._receive_locks[int.from_bytes(digest[:2]) % 256]:
            await self._process_native_event(event)

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name=ADAPTER_ID,
            description=PLUGIN_DISPLAY_NAME,
            id=self.config.get("id", ADAPTER_ID),
            adapter_display_name=PLUGIN_DISPLAY_NAME,
            support_streaming_message=False,
        )

    def create_event(self, message: AstrBotMessage) -> WeBridgeEvent:
        # Debounce and other plugins re-inject messages through this factory.
        # The base Platform.create_event returns a generic event whose send()
        # only records metrics, so replies to re-injected messages silently
        # never reach WeChat.
        return WeBridgeEvent(message, self)

    def get_client(self):
        return self.client

    async def _process_native_event(self, event: dict):
        if self.stopped:
            return
        cursor = event["cursor"]
        if cursor_number(cursor) <= cursor_number(self.state.cursor):
            logger.debug(
                "微信消息适配器 SSE 事件忽略：cursor=%s reason=cursor_already_committed",
                cursor,
            )
            return
        data = event["data"]
        if event["kind"] == "message.recalled":
            thread, server_id, message_id = (
                data.get("threadId"),
                data.get("serverId"),
                data.get("messageId"),
            )
            if not isinstance(thread, str) or not thread:
                raise ValueError("invalid_recall_identity")
            if (
                not isinstance(server_id, str)
                or not server_id
                or (message_id is not None and not isinstance(message_id, str))
            ):
                raise ValueError("invalid_recall_identity")
            self.state.recall(thread, server_id, message_id)
            for active in list(self._events.values()):
                raw = active.message_obj.raw_message
                reference = raw.get("quote")
                if raw["threadId"] == thread and (raw.get("serverId") == server_id):
                    active.stop_event()
                    media_task = active.get_extra("webridge_media_task")
                    if media_task and not media_task.done():
                        media_task.cancel()
                elif (
                    raw["threadId"] == thread
                    and isinstance(reference, dict)
                    and reference.get("serverId") == server_id
                ):
                    active.set_extra("webridge_quote_invalidated", True)
                    active.stop_event()
                    if self.state.message_status(raw["id"]) == "queued":
                        self.state.mark_message(raw["id"], "no_reply")
                    media_task = active.get_extra("webridge_media_task")
                    if media_task and not media_task.done():
                        media_task.cancel()
            self.state.advance(cursor)
            return
        if event["kind"] != "message.committed":
            self.state.advance(cursor)
            return
        message_id, thread = data.get("messageId"), data.get("threadId")
        logger.debug(
            "微信消息适配器 收到消息事件：cursor=%s message=%s origin=%s",
            cursor,
            message_id,
            data.get("origin", "unknown"),
        )
        if (
            not isinstance(message_id, str)
            or not message_id
            or not isinstance(thread, str)
            or not thread
        ):
            raise ValueError("invalid_message_identity")
        raw = await self.client.message(message_id)
        if (
            raw.get("id") != message_id
            or raw.get("accountId") != self.native_config.account_id
            or raw.get("threadId") != thread
        ):
            raise ValueError("message_identity_mismatch")
        origin = data.get("origin", "unknown")
        kind = self._classify(raw, origin)
        fresh = self.state.accept(
            cursor,
            message_id,
            thread,
            raw.get("serverId"),
            "ignored" if kind == "ignore" else "queued",
        )
        if not fresh:
            logger.debug(
                "微信消息适配器 消息去重跳过：message=%s status=%s",
                message_id,
                self.state.message_status(message_id),
            )
        if not fresh or kind == "ignore":
            return
        wrapped = self.create_event(convert_message(raw, self.native_config, self.bot_self_id))
        self._events[message_id] = wrapped
        media_kind = self._media_kind(raw)
        if kind == "text" and media_kind is None:
            self._queue_event(wrapped)
        else:
            wrapped.set_extra("webridge_media_kind", media_kind)
            task = asyncio.create_task(self._process_media_message(raw, wrapped))
            wrapped.set_extra("webridge_media_task", task)
            self._media_tasks.add(task)
            task.add_done_callback(self._media_tasks.discard)

    async def _check_quote_original(
        self, raw: dict, wrapped: WeBridgeEvent, original_id: str
    ) -> None:
        media_kind = wrapped.get_extra("webridge_media_kind") or "image"
        quote = raw["quote"]
        original = await self.client.message(original_id)
        if (
            original.get("id") != original_id
            or original.get("accountId") != self.native_config.account_id
            or original.get("threadId") != raw["threadId"]
            or original.get("serverId") != quote["serverId"]
            or original.get("senderId") != quote["senderId"]
            or original.get("type") != {"image": 3, "voice": 34}[media_kind]
        ):
            raise MediaError("unavailable", media_kind=media_kind)
        if (
            original.get("recall") is not None
            or self.state.message_status(original_id) == "recalled"
        ):
            wrapped.set_extra("webridge_quote_invalidated", True)
            raise MediaError("recalled")

    async def _fetch_quote_original(self, raw, wrapped, guard, media_kind="image"):
        """Resolve an explicit quote to a verified saved message in the same chat."""
        quote = raw["quote"]
        if not all(
            isinstance(quote.get(key), str) and quote[key] for key in ("serverId", "senderId")
        ):
            raise MediaError("unavailable", media_kind=media_kind)
        original_id = self.state.find_message_id(raw["threadId"], quote["serverId"])
        if original_id is None:
            raise MediaError("expired", media_kind=media_kind)
        guard()
        await self._check_quote_original(raw, wrapped, original_id)
        guard()
        loader = {
            "image": self.client.fetch_image,
            "voice": self.client.fetch_voice,
        }[media_kind]
        payload = await loader(original_id, guard=guard)
        guard()
        await self._check_quote_original(raw, wrapped, original_id)
        current = await self.client.message(raw["id"])
        guard()
        if any(
            current.get(key) != raw.get(key) for key in ("id", "accountId", "threadId", "serverId")
        ):
            raise MediaError("unavailable", media_kind=media_kind)
        current_quote = current.get("quote")
        if current.get("recall") is not None:
            self.state.recall(raw["threadId"], raw.get("serverId"), raw["id"])
            raise MediaError("recalled", media_kind=media_kind)
        if (
            not isinstance(current_quote, dict)
            or any(current_quote.get(k) != quote.get(k) for k in ("serverId", "senderId", "type"))
            or current_quote.get("state") == "recalled"
        ):
            wrapped.set_extra("webridge_quote_invalidated", True)
            raise MediaError("recalled", media_kind=media_kind)
        wrapped.set_extra("webridge_quote_original_id", original_id)
        return payload

    async def _process_media_message(self, raw: dict, wrapped: WeBridgeEvent) -> None:
        message_id = raw["id"]
        kind = wrapped.get_extra("webridge_media_kind")
        quoted = raw.get("type") == 49 and raw.get("appMessageType") == 57

        def guard():
            if (
                self.stopped
                or self._state_closed
                or wrapped.is_stopped()
                or wrapped.get_extra("webridge_quote_invalidated")
                or self.state.message_status(message_id) != "queued"
            ):
                raise MediaError("recalled", media_kind=kind)

        try:
            async with self._media_semaphore:
                guard()
                async with asyncio.timeout(self.client.media_timeout):
                    image_bytes = voice_bytes = None
                    if quoted and raw["quote"].get("state") == "recalled":
                        raise MediaError("recalled", media_kind=kind)
                    if kind == "image":
                        try:
                            image_bytes = await self.client.fetch_image(
                                message_id, quoted=quoted, guard=guard
                            )
                        except QuotedMediaMetadataError:
                            image_bytes = await self._fetch_quote_original(raw, wrapped, guard)
                    elif kind == "voice":
                        voice_bytes = (
                            await self._fetch_quote_original(raw, wrapped, guard, "voice")
                            if quoted
                            else await self.client.fetch_voice(message_id, guard=guard)
                        )
                    else:
                        raise MediaError("unsupported_format")
                    latest = await self.client.message(message_id)
                    guard()
                    if any(
                        latest.get(k) != raw.get(k)
                        for k in ("id", "accountId", "threadId", "serverId")
                    ):
                        raise MediaError("unavailable", media_kind=kind)
                    if latest.get("recall") is not None:
                        self.state.recall(raw["threadId"], raw.get("serverId"), message_id)
                        raise MediaError("recalled", media_kind=kind)
                enriched = convert_message(
                    raw,
                    self.native_config,
                    self.bot_self_id,
                    image_bytes=image_bytes,
                    voice_bytes=voice_bytes,
                )
                delivered = self.create_event(enriched)
                delivered.set_extra("webridge_media_kind", kind)
                original_id = wrapped.get_extra("webridge_quote_original_id")
                if original_id:
                    delivered.set_extra("webridge_quote_original_id", original_id)
                self._queue_event(delivered)
        except (MediaError, NativeAPIError, TimeoutError) as error:
            reason = error.reason if isinstance(error, MediaError) else "unavailable"
            logger.warning("微信消息适配器 媒体传输失败：message=%s reason=%s", message_id, reason)
            if not self._state_closed and self.state.message_status(message_id) == "queued":
                if reason == "recalled":
                    if not quoted:
                        self.state.recall(raw["threadId"], raw.get("serverId"), message_id)
                    else:
                        wrapped.set_extra("webridge_quote_invalidated", True)
                        self.state.mark_message(message_id, "no_reply")
                    wrapped.stop_event()
                elif quoted and not wrapped.is_stopped() and not self.stopped:
                    # Preserve the user's actual text when referenced media is unavailable.
                    wrapped.set_extra("webridge_media_error", reason)
                    try:
                        self._queue_event(wrapped)
                    except RuntimeError:
                        logger.error("微信消息适配器 引用文本交付失败：message=%s", message_id)
                else:
                    self.state.mark_message(message_id, "failed")
        except asyncio.CancelledError:
            if not self._state_closed and self.state.message_status(message_id) == "queued":
                self.state.mark_message(message_id, "needs_review")
            if self.stopped:
                raise
        except Exception:
            if not self._state_closed:
                self.state.mark_message(message_id, "needs_review")
            logger.exception("微信消息适配器 媒体交付失败：message=%s", message_id)
        finally:
            wrapped.cleanup_temporary_local_files()

    async def run(self):
        self._run_task = asyncio.current_task()
        delay = self.reconnect_delay
        try:
            await self.client.open()
            while not self.stopped:
                try:
                    if self.managed:
                        await self._connect_managed()
                    # Recheck on every connection, including initial transient failures.
                    account = await self.client.account()
                    if account.get("id") != self.native_config.account_id:
                        raise ValueError("account_identity_mismatch")
                    status = await self.client.status()
                    self.state.bind_server(status.get("serverId"))
                    self.bot_self_id = account.get("ownUsername") or self.native_config.account_id
                    if not account.get("authenticated") or account.get("connection") != "online":
                        logger.warning("微信消息适配器 账号尚未在线；等待 Native 恢复连接")
                    async for event in self.client.events(self.state.cursor):
                        await self.process_native_event(event)
                        delay = self.reconnect_delay
                    if not self.stopped:
                        logger.warning("微信消息适配器 SSE 已断开，使用已保存游标重连")
                except NativeAPIError as error:
                    if error.status == 410 or (not self.managed and error.status in {401, 403}):
                        logger.error(
                            "微信消息适配器 停止接收：HTTP %s (%s)；请检查认证或游标",
                            error.status,
                            error.code,
                        )
                        raise
                    logger.warning("微信消息适配器 暂时不可达，将重连（HTTP %s）", error.status)
                except RuntimeUnavailable as error:
                    logger.warning("微信消息适配器 等待内置微信引擎：%s", error)
                except ValueError:
                    logger.error("微信消息适配器 收到无效消息身份；保留游标，等待重试")
                if not self.stopped:
                    await asyncio.sleep(delay + random.uniform(0, delay * 0.2))
                    delay = min(30, delay * 2)
        except asyncio.CancelledError:
            if not self.stopped:
                raise
        finally:
            await self.client.close()

    async def send_chain(self, thread: str, chain, *, source=None, event=None) -> bool:
        if self.stopped:
            raise RuntimeError("微信消息适配器 已停止")
        if self.state is None:
            raise RuntimeError("微信平台尚未就绪，请在微信消息适配器界面完成登录")
        logger.info(
            "微信消息适配器 收到发送请求：message=%s components=%s",
            source.get("id") if source else None,
            ",".join(type(part).__name__ for part in chain.chain),
        )
        task = asyncio.current_task()
        self._send_tasks.add(task)
        try:
            async with self._send_lock:
                if self.stopped:
                    raise RuntimeError("微信消息适配器 已停止")
                if event and event.get_extra("webridge_quote_invalidated"):
                    event.stop_event()
                    return False
                parts, mentions, reply_id = serialize_chain(
                    chain, group=thread.endswith("@chatroom")
                )
                if not parts:
                    return False
                for part in parts:
                    if isinstance(part, str) and len(part.encode()) > 65536:
                        raise ValueError("Native 文本超过 64 KiB，未发送")
                    if (
                        isinstance(part, str)
                        and reply_id is not None
                        and len(part.encode()) > 16384
                    ):
                        raise ValueError("Native 引用回复文本超过 16 KiB，未发送")
                if self.client.closed:
                    raise ValueError("Native 客户端未连接，未提交发送")
                message_id = source["id"] if source else None
                if source:
                    source_status = self.state.message_status(message_id)
                    if source_status in {"needs_review", "failed", "partial"}:
                        raise RuntimeError("此消息的回复需人工检查，禁止自动重发")
                    if source_status == "recalled":
                        if event:
                            event.stop_event()
                        return False
                if mentions:
                    await self.client.prepare_group_mentions(
                        thread, [self.bot_self_id, *mentions.get("userIds", [])]
                    )
                if source:
                    current = await self.client.message(message_id)
                    if (
                        current.get("id") != message_id
                        or current.get("accountId") != self.native_config.account_id
                        or current.get("threadId") != thread
                        or current.get("serverId") != source.get("serverId")
                    ):
                        raise ValueError("reply_source_identity_mismatch")
                    if self.state.message_status(message_id) in {
                        "needs_review",
                        "failed",
                        "partial",
                    }:
                        raise RuntimeError("此消息的回复需人工检查，禁止自动重发")
                    if (
                        current.get("recall") is not None
                        or self.state.message_status(message_id) == "recalled"
                    ):
                        self.state.recall(thread, source.get("serverId"), message_id)
                        if event:
                            event.stop_event()
                        return False
                    original_quote = source.get("quote")
                    current_quote = current.get("quote")
                    if isinstance(original_quote, dict) and (
                        not isinstance(current_quote, dict)
                        or current_quote.get("serverId") != original_quote.get("serverId")
                        or (
                            original_quote.get("state") != "recalled"
                            and current_quote.get("state") == "recalled"
                        )
                        or (event and event.get_extra("webridge_quote_invalidated"))
                    ):
                        if source_status == "queued":
                            self.state.mark_message(message_id, "no_reply")
                        if event:
                            event.stop_event()
                        return False
                    original_id = event.get_extra("webridge_quote_original_id") if event else None
                    if original_id:
                        try:
                            await self._check_quote_original(source, event, original_id)
                        except (MediaError, NativeAPIError):
                            event.set_extra("webridge_quote_invalidated", True)
                            event.stop_event()
                            if self.state.message_status(message_id) == "queued":
                                self.state.mark_message(message_id, "no_reply")
                            return False
                quote = None
                if reply_id is not None:
                    target = (
                        current
                        if source and reply_id == message_id
                        else await self.client.message(reply_id)
                    )
                    if (
                        target.get("id") != reply_id
                        or target.get("accountId") != self.native_config.account_id
                        or target.get("threadId") != thread
                        or not isinstance(target.get("serverId"), str)
                        or not target["serverId"]
                        or target.get("recall") is not None
                        or self.state.message_status(reply_id) == "recalled"
                    ):
                        raise ValueError("Native 引用来源不可用、已撤回或不属于当前账号和聊天")
                    quote = {"messageId": reply_id, "serverId": target["serverId"]}
                    # Another target read may overlap source recall or send failure.
                    if source and self.state.message_status(message_id) == "recalled":
                        if event:
                            event.stop_event()
                        return False
                    if source and self.state.message_status(message_id) in {
                        "needs_review",
                        "failed",
                        "partial",
                    }:
                        raise RuntimeError("此消息的回复需人工检查，禁止自动重发")
                if event and event.get_extra("webridge_quote_invalidated"):
                    event.stop_event()
                    return False

                async def check_current_source():
                    # Downloads/uploads can overlap recalls; recheck before every submission.
                    if self.stopped:
                        raise RuntimeError("微信消息适配器 已停止")
                    if source:
                        status = self.state.message_status(message_id)
                        if status in {"needs_review", "failed", "partial", "recalled"}:
                            raise ValueError("回复来源已撤回或需人工检查，未提交发送")
                        latest = await self.client.message(message_id)
                        if self.state.message_status(message_id) in {
                            "needs_review",
                            "failed",
                            "partial",
                            "recalled",
                        }:
                            raise ValueError("回复来源已撤回或需人工检查，未提交发送")
                        if (
                            latest.get("id") != message_id
                            or latest.get("accountId") != self.native_config.account_id
                            or latest.get("threadId") != thread
                            or latest.get("serverId") != source.get("serverId")
                        ):
                            raise ValueError("reply_source_identity_mismatch")
                        if latest.get("recall") is not None:
                            self.state.recall(thread, source.get("serverId"), message_id)
                            if event:
                                event.stop_event()
                            raise ValueError("回复来源已撤回，未提交发送")
                        original_quote = source.get("quote")
                        latest_quote = latest.get("quote")
                        if isinstance(original_quote, dict) and (
                            not isinstance(latest_quote, dict)
                            or latest_quote.get("serverId") != original_quote.get("serverId")
                            or latest_quote.get("state") == "recalled"
                        ):
                            raise ValueError("引用来源已变化或撤回，未提交发送")
                        original_id = (
                            event.get_extra("webridge_quote_original_id") if event else None
                        )
                        if original_id:
                            await self._check_quote_original(source, event, original_id)
                    if event and event.get_extra("webridge_quote_invalidated"):
                        raise ValueError("引用来源已失效，未提交发送")
                    if quote:
                        target = await self.client.message(quote["messageId"])
                        if (
                            target.get("id") != quote["messageId"]
                            or target.get("accountId") != self.native_config.account_id
                            or target.get("threadId") != thread
                            or target.get("serverId") != quote["serverId"]
                            or target.get("recall") is not None
                            or self.state.message_status(quote["messageId"]) == "recalled"
                        ):
                            raise ValueError("Native 引用来源已失效，未提交发送")

                for index, part in enumerate(parts):
                    # Resolve each image with bounded memory, before recording an intent.
                    # No later component is attempted after a failure or unknown outcome.
                    if isinstance(part, Nodes):
                        try:
                            forward_nodes = await self.client.prepare_forward(part.nodes)
                        except BaseException:
                            if index and source and not self._state_closed:
                                self.state.mark_message(message_id, "partial")
                            raise
                    elif not isinstance(part, str):
                        loader = (
                            load_outgoing_voice
                            if isinstance(part, Record)
                            else (
                                load_outgoing_video
                                if isinstance(part, Video)
                                else load_outgoing_image
                            )
                        )
                        try:
                            data, content_type = await loader(part)
                        except BaseException as error:
                            if isinstance(error, ValueError):
                                logger.warning(
                                    "微信消息适配器 媒体准备失败，未提交发送："
                                    "message=%s media=%s reason=%s",
                                    message_id,
                                    type(part).__name__,
                                    error,
                                )
                            if index and source and not self._state_closed:
                                self.state.mark_message(message_id, "partial")
                            raise
                    key = "astrbot-" + uuid.uuid4().hex
                    self.state.begin_send(key, message_id, thread)

                    def record_voice_progress(chunks):
                        self.state.record_voice_chunks(key, chunks)

                    logger.debug(
                        "微信消息适配器 开始发送回复：message=%s receipt_key=%s",
                        message_id,
                        key,
                    )
                    try:
                        if index or not isinstance(part, str):
                            await check_current_source()
                        if isinstance(part, str):
                            receipt = await self.client.send_text(
                                thread, part, key, mentions, quote=quote
                            )
                            mentions, quote = None, None
                        elif isinstance(part, Nodes):
                            receipt = await self.client.send_forward(
                                thread,
                                forward_nodes,
                                key,
                                record_send=lambda send_id: self.state.record_send_id(key, send_id),
                            )
                        elif isinstance(part, Record):
                            receipt = await self.client.send_voice(
                                thread,
                                data,
                                content_type,
                                key,
                                record_job=lambda job_id: self.state.record_send_id(key, job_id),
                                before_send=check_current_source,
                                record_chunks=record_voice_progress,
                            )
                        elif isinstance(part, Video):
                            receipt = await self.client.send_video(
                                thread,
                                data,
                                content_type,
                                key,
                                record_job=lambda job_id: self.state.record_send_id(key, job_id),
                                before_send=check_current_source,
                            )
                        else:
                            receipt = await self.client.send_image(
                                thread,
                                data,
                                content_type,
                                key,
                                record_job=lambda job_id: self.state.record_send_id(key, job_id),
                                before_send=check_current_source,
                            )
                    except BaseException as error:
                        state = "needs_review"
                        if isinstance(error, SendError) and error.state in {
                            "failed",
                            "cancelled",
                            "partial",
                        }:
                            state = error.state
                        elif isinstance(error, ValueError) or (
                            isinstance(error, NativeAPIError)
                            and 400 <= error.status < 500
                            and error.status not in {408, 429}
                        ):
                            state = "failed"
                        send_id = error.send_id if isinstance(error, SendError) else None
                        self.state.finish_send(key, state, send_id)
                        if message_id:
                            self.state.mark_message(
                                message_id,
                                "partial"
                                if any(
                                    c["state"] == "accepted" for c in self.state.voice_chunks(key)
                                )
                                else "failed"
                                if state == "cancelled"
                                else state,
                            )
                        logger.error(
                            "微信消息适配器 回复未确认；状态 %s，回执键 %s。"
                            "请检查状态库，勿盲目重发",
                            state,
                            key,
                        )
                        raise
                    self.state.finish_send(key, "accepted", receipt["id"])
                    if message_id:
                        self.state.mark_message(message_id, "replied")
                    logger.info(
                        "微信消息适配器 回复已被 Native 接受：message=%s send=%s",
                        message_id,
                        receipt["id"],
                    )
                return True
        finally:
            self._send_tasks.discard(task)

    async def send_by_session(self, session, message_chain):
        thread = session_target(session.session_id, self.native_config.account_id)
        if session.platform_id != self.meta().id:
            raise ValueError("会话不属于当前机器人实例")
        if await self.send_chain(thread, message_chain):
            await super().send_by_session(session, message_chain)

    async def terminate(self):
        async with self._terminate_lock:
            await self._terminate()

    async def _terminate(self):
        if self._state_closed:
            return
        self.stopped = True
        current = asyncio.current_task()
        tasks = {task for task in (self._send_tasks | self._media_tasks) if task is not current}
        if self._run_task and self._run_task is not current:
            tasks.add(self._run_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for event in list(self._events.values()):
            event.stop_event()
            if not event._execution_active:
                event.cleanup_temporary_local_files()
        await self.client.close()
        if self.state is not None:
            self.state.close()
        self._state_closed = True
