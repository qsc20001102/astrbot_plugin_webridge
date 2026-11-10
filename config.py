"""Connection settings only. Reply policy belongs to AstrBot's selected profile."""

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

PLUGIN_ID = "astrbot_plugin_webridge"
PLUGIN_DISPLAY_NAME = "微信消息适配器"
ADAPTER_ID = "webridge"

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_VOICE_BYTES = 25 * 1024 * 1024 - 17
MAX_VOICE_SECONDS = 300
VOICE_SEND_TIMEOUT = 25 * 60
# The existing Native CDN descriptor uses u32 byte lengths. Leave room for
# AES-ECB padding; this is a representation boundary, not a 50 MiB send policy.
VIDEO_PROTOCOL_MAX_BYTES = (1 << 32) - 17
VIDEO_SEND_TIMEOUT = 60 * 60
VIDEO_DOWNLOAD_TIMEOUT = 180
MEDIA_DOWNLOAD_TIMEOUT = 60
MAX_CONCURRENT_MEDIA_TASKS = 3

DEFAULT_CONFIG = {
    "id": ADAPTER_ID,
    "type": ADAPTER_ID,
    "enable": False,
    "account_id": "",
}

CONFIG_METADATA = {
    "logo_token": {"type": "string", "invisible": True},
    "account_id": {
        "type": "string",
        "description": "微信账号",
        "hint": "推荐在插件的微信管理界面扫码并一键接入；仅有一个已登录账号时可留空",
    },
}


@dataclass(frozen=True)
class NativeConfig:
    api_url: str
    account_id: str
    token: str = field(repr=False)
    token_file: str = field(repr=False)

    @classmethod
    def from_dict(cls, data: dict) -> "NativeConfig":
        # Only connection settings are parsed; message policy belongs to AstrBot.
        url = str(data.get("server_url", "")).strip().rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("server_url 必须是无认证信息、查询参数或片段的 HTTP(S) 地址")
        try:
            parsed.port
        except ValueError:
            raise ValueError("server_url 端口无效") from None
        account = data.get("account_id", "")
        if not isinstance(account, str) or not account.strip():
            raise ValueError("account_id 必填")
        if not isinstance(data.get("token_file", ""), str):
            raise ValueError("token_file 必须是文件路径字符串")
        return cls(
            api_url=url if parsed.path.endswith("/v1") else url + "/v1",
            account_id=account.strip(),
            token=data.get("token", ""),
            token_file=data.get("token_file", ""),
        )

    def load_token(self) -> str:
        token = self.token
        if self.token_file:
            try:
                with Path(self.token_file).open() as f:
                    token = f.read(8193).rstrip("\r\n")
            except (OSError, TypeError, ValueError):
                raise ValueError("token_file 不可读，请检查容器挂载与权限") from None
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 8192
            or any(ord(c) < 33 or ord(c) > 126 for c in token)
        ):
            raise ValueError("必须配置有效的 token 或 token_file（不含空格或控制字符）")
        return token
