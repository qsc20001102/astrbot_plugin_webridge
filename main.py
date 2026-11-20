"""Register the transport, account management and profile-controlled sessions."""

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.message_components import Node, Nodes, Reply
from astrbot.api.star import Context, Star
from astrbot.core.event_bus import UNIQUE_SESSION_ID_BUILDERS

from .adapter import WeBridgeAdapter  # noqa: F401
from .config import ADAPTER_ID, PLUGIN_ID
from .event import session_id
from .forward import forward_text, limit_forward_chain, parser_settings
from .native_ui import NativeView
from .runtime import RuntimeUnavailable, close_runtime, get_runtime
from .video import VideoPolicy, set_plugin_config


def webridge_session_builder(event) -> str:
    """Called by AstrBot after it selects the actual message's configuration."""
    return session_id(
        event.adapter.native_config.account_id, event.get_group_id(), event.get_sender_id()
    )


class WeBridgePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config
        # The platform adapter reads VideoPolicy later; publish the plugin
        # config before PlatformManager creates adapters.
        set_plugin_config(config)
        native = NativeView(context, get_runtime())
        for route, handler, method, description in [
            ("status", native.status, "GET", "微信账号状态"),
            ("start", native.start, "POST", "启动内置微信引擎"),
            ("accounts", native.create_account, "POST", "添加微信账号"),
            ("login", native.login, "POST", "获取微信登录二维码"),
            ("login-status", native.login_status, "GET", "扫码登录进度"),
            ("cancel-login", native.cancel_login, "POST", "取消扫码登录"),
            ("logout", native.logout, "POST", "退出微信登录"),
            ("platform", native.platform, "POST", "一键接入 AstrBot"),
        ]:
            context.register_web_api(f"/{PLUGIN_ID}/native/{route}", handler, [method], description)

    @filter.on_waiting_llm_request(priority=10000)
    async def prepare_forward_content(self, event):
        if event.get_platform_name() != ADAPTER_ID:
            return
        components = event.get_messages()
        if not any(
            isinstance(part, (Node, Nodes))
            or (
                isinstance(part, Reply)
                and any(isinstance(item, (Node, Nodes)) for item in part.chain or [])
            )
            for part in components
        ):
            return
        settings = parser_settings(self.context.get_config(event.unified_msg_origin))
        event.message_obj.message = limit_forward_chain(components, settings)

    @filter.on_llm_request()
    async def attach_forward_content(self, event, request):
        if event.get_platform_name() != ADAPTER_ID:
            return
        nodes = [part for part in event.get_messages() if isinstance(part, (Node, Nodes))]
        if not nodes:
            return
        settings = parser_settings(self.context.get_config(event.unified_msg_origin))
        text = forward_text(limit_forward_chain(nodes, settings), settings)
        if text:
            request.prompt = (request.prompt or "") + "\n" + text

    async def initialize(self):
        # Re-publish in case the adapter is created during this hook, and
        # refresh any live adapter after a plugin-config save/reload.
        set_plugin_config(getattr(self, "config", None))
        manager = getattr(self.context, "platform_manager", None)
        if manager is not None:
            for inst in manager.get_insts():
                if isinstance(inst, WeBridgeAdapter):
                    inst.video_policy = VideoPolicy.load(inst._state_root)
        # v4.29.4 has no public registration helper for custom session builders.
        # Use its existing registry without changing EventBus or default settings.
        existing = UNIQUE_SESSION_ID_BUILDERS.get(ADAPTER_ID)
        if existing is not None and existing is not webridge_session_builder:
            raise RuntimeError("webridge 的会话隔离适配已被其他组件注册")
        UNIQUE_SESSION_ID_BUILDERS[ADAPTER_ID] = webridge_session_builder
        try:
            await get_runtime().start()
        except RuntimeUnavailable as error:
            # Keep the account UI available so an installation error is visible.
            logger.warning("微信消息适配器 内置引擎尚未就绪：%s", error)
        manager = getattr(self.context, "platform_manager", None)
        # PlatformManager creates webchat after its initial platform pass. On a
        # plugin update it is already present; on initial startup AstrBot will
        # load our configured bots itself after all plugins initialize.
        if manager is not None and any(
            inst.meta().name == "webchat" for inst in manager.get_insts()
        ):
            for saved in self.context.get_config().get("platform", []):
                if saved.get("type") == ADAPTER_ID and saved.get("enable"):
                    current = self.context.get_platform_inst(saved["id"])
                    if current is None or type(current) is not WeBridgeAdapter:
                        await manager.reload(saved)
        logger.info(
            "微信消息适配器 已加载集成版：内置 Native，图片与聊天记录收发，视频发送，收到视频忽略；"
            "构建 integrated-20261119-video-quality"
        )

    async def terminate(self):
        try:
            manager = getattr(self.context, "platform_manager", None)
            if manager is not None:
                for inst in list(manager.get_insts()):
                    if inst.meta().name != ADAPTER_ID:
                        continue
                    try:
                        await manager.terminate_platform(inst.meta().id)
                    except Exception:
                        logger.exception("微信消息适配器 停止消息平台失败，继续清理引擎")
        finally:
            if UNIQUE_SESSION_ID_BUILDERS.get(ADAPTER_ID) is webridge_session_builder:
                UNIQUE_SESSION_ID_BUILDERS.pop(ADAPTER_ID)
            await close_runtime()
