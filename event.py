"""AstrBot message conversion and event lifecycle."""

import asyncio
import base64
import re
from datetime import UTC, datetime
from urllib.parse import quote, unquote

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import (
    At,
    AtAll,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.api.platform import AstrBotMessage, Group, MessageMember, MessageType

from .forward import parse_forward_xml, validate_forward_nodes


def message_timestamp(raw: dict) -> int | None:
    value = raw.get("createdAt")
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return int(stamp.timestamp())
    except (ValueError, OverflowError):
        return None


def session_id(account: str, thread: str, sender: str | None = None) -> str:
    group = thread.endswith("@chatroom")
    parts = [quote(account, safe=""), "group" if group else "private", quote(thread, safe="")]
    if group and sender:
        parts.append(quote(sender, safe=""))
    return "|".join(parts)


def session_target(value: str, account: str) -> str:
    parts = value.split("|")
    if (
        len(parts) not in {3, 4}
        or unquote(parts[0]) != account
        or parts[1] not in {"group", "private"}
        or not parts[2]
    ):
        raise ValueError("会话不属于当前 Native 账号或会话格式无效")
    thread = unquote(parts[2])
    if (parts[1] == "group") != thread.endswith("@chatroom"):
        raise ValueError("会话类型与目标不匹配")
    if parts[1] == "private" and len(parts) != 3:
        raise ValueError("无效私聊会话")
    return thread


def convert_message(
    raw: dict,
    config,
    self_id: str,
    *,
    image_bytes: bytes | None = None,
    voice_bytes: bytes | None = None,
) -> AstrBotMessage:
    result = AstrBotMessage()
    thread, sender = raw["threadId"], raw["senderId"]
    group = thread.endswith("@chatroom")
    result.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    result.self_id = self_id
    # The EventBus selects a profile using this shared origin before applying
    # that profile's unique_session setting through our registered builder.
    result.session_id = session_id(config.account_id, thread)
    result.message_id = raw["id"]
    result.sender = MessageMember(user_id=sender, nickname=sender)
    result.group = Group(group_id=thread) if group else None
    text = raw["body"] if isinstance(raw.get("body"), str) else ""
    if raw.get("type") in {3, 34, 43} or (raw.get("type") == 49 and raw.get("appMessageType") == 6):
        # Native media bodies can contain protocol metadata, never user prompts.
        text = ""
    mentions = raw.get("mentions")
    mentioned = (
        group
        and isinstance(mentions, dict)
        and mentions.get("state") == "known"
        and mentions.get("self") is True
    )
    mention_components = []
    if group and isinstance(mentions, dict) and mentions.get("state") == "known":
        if mentioned:
            mention_components.append(At(qq=self_id))
        users = mentions.get("userIds")
        if isinstance(users, list):
            seen = {self_id} if mentioned else set()
            for user in users:
                if isinstance(user, str) and user and user not in seen:
                    mention_components.append(At(qq=user))
                    seen.add(user)
        if mentions.get("all") is True:
            mention_components.append(AtAll())
    if mention_components:
        # WeChat terminates visible mention labels with U+2005. Keep the raw
        # body intact, and never treat an ordinary typed @name as metadata.
        text = re.sub(r"^(?:@[^\u2005\r\n]+\u2005[ \t]*)+", "", text)
    result.message_str = text
    result.message = mention_components
    is_forward = raw.get("type") == 49 and raw.get("appMessageType") == 19
    if is_forward:
        text = "[聊天记录]"
        result.message_str = text
        try:
            result.message.append(parse_forward_xml(raw.get("forwardXml")))
        except ValueError:
            result.message.append(Nodes([Node(content=[Plain("[聊天记录内容不可用]")])]))
    quote = raw.get("quote")
    if raw.get("type") == 49 and raw.get("appMessageType") == 57 and isinstance(quote, dict):
        state = quote.get("state")
        quoted_text = "引用消息内容不可用"
        if image_bytes is not None and quote.get("type") == 3:
            if (
                state == "embedded"
                and isinstance(quote.get("embeddedText"), str)
                and quote["embeddedText"].strip()
            ):
                quoted_text = quote["embeddedText"][:16384]
            else:
                quoted_text = "[图片]"
        elif quote.get("type") == 43 and state != "recalled":
            quoted_text = "[视频]"
        elif voice_bytes is not None and quote.get("type") == 34 and state != "recalled":
            quoted_text = "[语音]"
        elif state == "recalled":
            quoted_text = "引用消息已撤回"
        elif state == "unsupported":
            quoted_text = "不支持此引用消息的内容"
        elif state == "embedded" and isinstance(quote.get("embeddedText"), str):
            quoted_text = quote["embeddedText"][:16384] or quoted_text
        quote_chain = [Plain(quoted_text)]
        if quote.get("embeddedForwardXml") and state != "recalled":
            try:
                quote_chain = [parse_forward_xml(quote["embeddedForwardXml"])]
                quoted_text = "[聊天记录]"
            except ValueError:
                quote_chain = [Plain("[聊天记录内容不可用]")]
                quoted_text = "[聊天记录内容不可用]"
        result.message.append(
            Reply(
                id=quote.get("serverId") if isinstance(quote.get("serverId"), str) else "",
                sender_id=quote.get("senderId") if isinstance(quote.get("senderId"), str) else "",
                message_str=quoted_text,
                chain=quote_chain,
            )
        )
    if not is_forward and (text or (image_bytes is None and voice_bytes is None)):
        result.message.append(Plain(text))
    if image_bytes is not None:
        result.message.append(Image.fromBytes(image_bytes))
    if voice_bytes is not None:
        result.message.append(Record.fromBase64(base64.b64encode(voice_bytes).decode("ascii")))
    result.raw_message = raw
    result.timestamp = message_timestamp(raw) or int(datetime.now(UTC).timestamp())
    return result


def serialize_chain(
    chain: MessageChain, *, group: bool
) -> tuple[list[str | Image | Record | Video | Nodes], dict | None, str | None]:
    """Keep media between contiguous text runs; metadata belongs to the first text run."""
    parts, text, users, all_members = [], [], [], False
    reply_id = None

    def flush_text():
        value = "".join(text)
        if value.strip():
            parts.append(value)
        text.clear()

    for index, component in enumerate(chain.chain):
        if index >= 1024:
            raise ValueError("消息链组件过多")
        if isinstance(component, (Node, Nodes)):
            flush_text()
            nodes = component.nodes if isinstance(component, Nodes) else [component]
            validate_forward_nodes(nodes)
            if parts and isinstance(parts[-1], Nodes):
                parts[-1].nodes.extend(nodes)
                validate_forward_nodes(parts[-1].nodes)
            else:
                parts.append(Nodes(list(nodes)))
            continue
        if isinstance(component, Plain):
            text.append(component.text)
        elif isinstance(component, (Image, Record, Video)):
            flush_text()
            parts.append(component)
        elif isinstance(component, At):
            user = str(component.qq)
            if user == "all":
                all_members = True
            elif user not in users:
                users.append(user)
        elif isinstance(component, AtAll):
            all_members = True
        elif isinstance(component, Reply):
            target = str(component.id).strip()
            if not target or (reply_id is not None and reply_id != target):
                raise ValueError("Native 引用回复需要且只能指定一条原消息")
            reply_id = target
        else:
            raise ValueError(
                "微信消息适配器 仅支持发送文本、图片、语音、视频、引用和群 @，"
                "以及这些组件的转发节点"
            )
    flush_text()
    if (users or all_members) and not group:
        users, all_members = [], False
    if users and all_members:
        raise ValueError("不能同时 @个人和 @全体")
    mentions = {"all": True} if all_members else {"userIds": users} if users else None
    if parts and not any(isinstance(part, str) for part in parts) and (mentions or reply_id):
        raise ValueError("Native 媒体接口不支持引用或 @，请在消息链中添加文本承载这些信息")
    return parts, mentions, reply_id


def serialize_text(chain: MessageChain, *, group: bool) -> tuple[str, dict | None, str | None]:
    parts, mentions, reply_id = serialize_chain(chain, group=group)
    if any(not isinstance(part, str) for part in parts):
        raise ValueError("媒体消息需要通过 send_chain 发送")
    return "".join(parts), mentions, reply_id


class WeBridgeEvent(AstrMessageEvent):
    def __init__(self, message, adapter):
        super().__init__(message.message_str, message, adapter.meta(), message.session_id)
        self.adapter = adapter

    def cleanup_temporary_local_files(self) -> None:
        super().cleanup_temporary_local_files()
        # Called by AstrBot's event_context when the pipeline completes, even if
        # the selected profile rejected this event before it produced a reply.
        if (
            self._execution_active
            and not self.adapter._state_closed
            and self.adapter.state.message_status(self.message_obj.message_id) == "queued"
        ):
            self.adapter.state.mark_message(self.message_obj.message_id, "no_reply")

    async def send(self, message: MessageChain) -> None:
        if self.adapter.stopped:
            raise RuntimeError("微信消息适配器 已停止")
        raw = (
            self.message_obj.raw_message if isinstance(self.message_obj.raw_message, dict) else None
        )
        status = self.adapter.state.message_status(self.message_obj.message_id)
        if status == "recalled":
            self.stop_event()
            return
        if status in {"needs_review", "failed", "partial"}:
            raise RuntimeError("此消息的回复需人工检查，禁止自动重发")
        try:
            sent = await self.adapter.send_chain(
                raw["threadId"]
                if raw
                else session_target(
                    self.message_obj.session_id,
                    self.adapter.native_config.account_id,
                ),
                message,
                source=raw,
                event=self,
            )
        except (Exception, asyncio.CancelledError) as error:
            # send_chain records every submitted/uncertain intent. A local
            # validation or download failure before submission can safely be
            # followed by the calling plugin's text fallback.
            if not self.adapter._state_closed and (
                not isinstance(error, ValueError) or status == "replied"
            ):
                current = self.adapter.state.message_status(self.message_obj.message_id)
                if current not in {
                    "failed",
                    "needs_review",
                    "partial",
                    "recalled",
                }:
                    self.adapter.state.mark_message(
                        self.message_obj.message_id,
                        "failed" if isinstance(error, ValueError) else "needs_review",
                    )
            raise
        if sent:
            if raw is None and status == "queued":
                self.adapter.state.mark_message(self.message_obj.message_id, "replied")
            await super().send(message)
        elif self.adapter.state.message_status(self.message_obj.message_id) == "queued":
            self.adapter.state.mark_message(self.message_obj.message_id, "no_reply")

    async def send_streaming(self, generator, use_fallback=False) -> None:
        # Native has no token streaming. Concatenate delta chains and send once.
        chain = MessageChain()
        async for part in generator:
            chain.chain.extend(part.chain)
        await self.send(chain)
