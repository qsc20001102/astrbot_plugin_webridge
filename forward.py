"""WeChat record XML and AstrBot's standard Node/Nodes components."""

from time import time

from astrbot.api.message_components import At, AtAll, Image, Node, Nodes, Plain, Reply
from astrbot.core.utils.quoted_message.chain_parser import ReplyChainParser
from astrbot.core.utils.quoted_message.settings import SETTINGS
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

MAX_FORWARD_NODES = 500
MAX_FORWARD_COMPONENTS = 1024
MAX_FORWARD_XML_BYTES = 1024 * 1024


def parse_forward_xml(xml: str) -> Nodes:
    """Decode each escaped/CDATA XML layer once, with aggregate resource bounds."""
    parsed_bytes = nodes_seen = 0

    def parse(value):
        nonlocal parsed_bytes
        parsed_bytes += len(value.encode("utf-8"))
        if parsed_bytes > 2 * MAX_FORWARD_XML_BYTES:
            raise ValueError("聊天记录内容过大")
        return ElementTree.fromstring(value, forbid_dtd=True)

    def field(node, name):
        return (node.findtext(name) or "").strip()

    def record(container, depth):
        nonlocal nodes_seen
        if depth > 16:
            return [Node(name="", content=[Plain("[聊天记录嵌套超过传输限制]")])]
        root = container.find("recordinfo")
        if root is None:
            root = parse(container.text or "")
        if root.tag != "recordinfo":
            raise ValueError("无效聊天记录结构")
        nodes = []
        for item in root.findall("datalist/dataitem"):
            nodes_seen += 1
            if nodes_seen > MAX_FORWARD_NODES:
                raise ValueError("聊天记录节点过多")
            name = field(item, "sourcename") or field(item, "dataitemsource/sourcename")
            uin = field(item, "dataitemsource/realchatname") or "0"
            stamp = field(item, "srcMsgCreateTime")
            try:
                stamp = max(0, int(stamp))
            except ValueError:
                stamp = 0
            datatype = item.get("datatype")
            nested = item.find("recordxml")
            if datatype == "17" or nested is not None:
                content = (
                    [Nodes(record(nested, depth + 1))]
                    if nested is not None
                    else [Plain("[嵌套聊天记录不可用]")]
                )
            elif datatype == "1":
                content = [Plain(field(item, "datadesc") or field(item, "datatitle"))]
            else:
                # CDN record attachments are not HTTP URLs. Never send their
                # keys/XML to the model or pretend that media was downloaded.
                label = {"2": "图片", "3": "语音", "4": "视频", "8": "文件", "37": "表情"}
                content = [Plain(f"[{label.get(datatype, '附件')}]")]
            nodes.append(Node(name=name, uin=uin, time=stamp, content=content))
        return nodes

    if not isinstance(xml, str) or len(xml.encode("utf-8")) > MAX_FORWARD_XML_BYTES:
        raise ValueError("聊天记录 XML 无效或过大")
    try:
        root = parse(xml)
        app = root.find("appmsg") if root.tag == "msg" else root if root.tag == "appmsg" else None
        if app is None or field(app, "type") != "19":
            raise ValueError("不是微信合并转发消息")
        container = app.find("recorditem")
        if container is None:
            raise ValueError("聊天记录缺少内容")
        return Nodes(record(container, 0))
    except (ElementTree.ParseError, DefusedXmlException) as error:
        raise ValueError("聊天记录 XML 解析失败") from error


def parser_settings(config):
    provider = config.get("provider_settings", {})
    overrides = provider.get("quoted_message_parser", {})
    return SETTINGS.with_overrides(overrides)


def limit_forward_chain(components, settings, *, depth=0, node_depth=0):
    """Apply the selected AstrBot profile after EventBus has chosen that profile."""
    if depth > settings.max_component_chain_depth:
        return []
    result = []
    for component in components:
        if isinstance(component, (Node, Nodes)):
            if node_depth > settings.max_forward_node_depth:
                result.append(Plain("[嵌套聊天记录已按 AstrBot 配置省略]"))
                continue
            nodes = component.nodes if isinstance(component, Nodes) else [component]
            limited = [
                Node(
                    name=node.name,
                    uin=node.uin,
                    time=node.time,
                    content=limit_forward_chain(
                        node.content, settings, depth=depth + 1, node_depth=node_depth + 1
                    ),
                )
                for node in nodes
            ]
            result.extend([Nodes(limited)] if isinstance(component, Nodes) else limited)
        elif isinstance(component, Reply):
            result.append(
                component.copy(
                    update={
                        "chain": limit_forward_chain(
                            component.chain or [], settings, depth=depth, node_depth=node_depth
                        )
                    }
                )
            )
        else:
            result.append(component)
    return result


def forward_text(components, settings) -> str:
    wrapper = Reply(id="", chain=components, message_str="")
    return ReplyChainParser(settings=settings).extract_text_from_reply_component(wrapper) or ""


def validate_forward_nodes(nodes):
    count = size = 0

    def walk(items, depth):
        nonlocal count, size
        if not items or depth > 8:
            raise ValueError("合并转发不能为空，嵌套最多 8 层")
        for node in items:
            count += 1
            if not isinstance(node, Node) or not node.content:
                raise ValueError("合并转发需要包含内容的 AstrBot Node")
            for value, limit in ((node.name or "", 1024), (node.uin or "0", 256)):
                size += len(value.encode("utf-8"))
                if len(value.encode("utf-8")) > limit:
                    raise ValueError("合并转发署名过长")
            for part in node.content:
                count += 1
                if isinstance(part, (Node, Nodes)):
                    walk(part.nodes if isinstance(part, Nodes) else [part], depth + 1)
                elif isinstance(part, (Plain, At, AtAll)):
                    text = (
                        part.text
                        if isinstance(part, Plain)
                        else part.name or str(part.qq)
                        if isinstance(part, At)
                        else "@全体成员"
                    )
                    size += len(text.encode("utf-8"))
                    if len(text.encode("utf-8")) > 65536 or any(
                        (ord(c) < 32 and c not in "\n\r\t") or c in "\ufffe\uffff" for c in text
                    ):
                        raise ValueError("合并转发文本无效或过长")
                elif not isinstance(part, Image):
                    raise ValueError("合并转发支持文本、图片、@显示文字和嵌套 Node/Nodes")
                if count > MAX_FORWARD_COMPONENTS or size > 128 * 1024:
                    raise ValueError("合并转发内容过多")
        if count > MAX_FORWARD_NODES * 2:
            raise ValueError("合并转发节点过多")

    walk(nodes, 0)


async def prepare_forward_nodes(nodes, upload_image):
    validate_forward_nodes(nodes)

    async def prepare(items):
        result = []
        for node in items:
            content = []
            for part in node.content:
                if isinstance(part, Plain):
                    content.append({"type": "text", "text": part.text})
                elif isinstance(part, At):
                    content.append({"type": "text", "text": "@" + (part.name or str(part.qq))})
                elif isinstance(part, AtAll):
                    content.append({"type": "text", "text": "@全体成员"})
                elif isinstance(part, Image):
                    content.append({"type": "image", "sourceId": await upload_image(part)})
                else:
                    nested = part.nodes if isinstance(part, Nodes) else [part]
                    content.append({"type": "forward", "nodes": await prepare(nested)})
            stamp = node.time or int(time())
            if not 0 < stamp <= 2147483647:
                raise ValueError("合并转发时间无效")
            result.append(
                {"name": node.name or "", "uin": node.uin or "0", "time": stamp, "content": content}
            )
        return result

    return await prepare(nodes)
