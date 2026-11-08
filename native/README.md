# 内置微信引擎

发行包内含 `bin/linux-x86_64/wx`，由插件启动和停止，不需要单独的 Native Docker 容器。

| 项目 | 固定版本 |
| --- | --- |
| 上游 | [thisnick/agent-wechat v1.5.1](https://github.com/thisnick/agent-wechat/tree/v1.5.1) |
| 提交 | `39af1eec84f4763e1257406f8c5fb848b31d82e9` |
| Rust | 1.93.1，默认 Cargo 功能 |
| 构建基础 | 官方 `rust:1.93.1-bookworm`，Dockerfile 固定镜像摘要 |
| 运行目标 | Linux x86_64，glibc 2.36+ |
| 补丁 | `patches/original-image.patch` |
| 二进制与补丁校验 | `manifest.json` |

媒体补丁保留 JPEG 原始字节；PNG 等图片生成完整尺寸的高质量 JPEG。预览图、缩略图仍分别准备，原图沿用已有 CDN 上传结构分块提交，并检查每块确认、文件 ID、完整长度和取消状态。旧任务的图片准备方式保留，避免改变旧幂等任务的结果。没有添加未经服务端支持的 API 参数。

视频播放副本会优先保留兼容的 H.264/AAC MP4；其他来源按源尺寸和帧率转换编码；取消 50 MiB/约 25 MiB 策略限制和按体积压低码率的处理，收到视频和缩略图不自动下载，详细条件见 [视频说明](../docs/video-quality.md)。清单的 `video_passthrough`、`video_size_policy` 和 `incoming_video_download` 标记与二进制 SHA-256 一起验证。

源码补丁包含合成图像回归测试：长图、50 MP JPEG、PNG 转换、完整原图分块重组/解密、预览只发送一次和中途取消。测试不等同于手机微信接收验收。

## 重建

在 Linux x86_64 安装 Docker、Git、Python 3，然后从插件根目录执行：

```bash
bash native/build-linux.sh
```

脚本下载固定的公开源码，检查 Rustfmt/Clippy 后应用补丁，执行格式、全工作区 Clippy 和相关测试，再编译、去除调试符号并更新清单。构建缓存留在 `.native-work`，不接触微信账号和现有容器数据。

Linux x86_64 二进制已纳入 Git，仓库和完整安装 ZIP 均可直接安装。需要重建时使用上述脚本；引擎与补丁的 SHA-256 必须和清单一致。检出上游源码时应关闭换行自动转换，尤其不要将 Native 的快照测试夹具转换为 CRLF。

## Linux 集成检查

大视频暂存回归可用 `--network none -e WEBRIDGE_OFFLINE_VIDEO_SMOKE=1` 运行 `python native/video_smoke.py`。它创建全新临时账号，通过真实 Native multipart 接口上传超过 50 MiB 的可解码 MP4，核对引擎能力、长度和 SHA-256，不登录微信、不提交发送。

二维码状态回归可在同一官方镜像中以 `--network none -e WEBRIDGE_OFFLINE_LOGIN_SMOKE=1` 运行 `python native/login_smoke.py`。它强制只存在回环网卡，创建全新临时账号，核对真实登录接口的初始 `starting` 状态、原幂等键复用和界面的终态判断；不接触微信服务器或发送消息。

在包含官方 AstrBot v4.29.4 和本插件的隔离环境中运行 `python native/smoke.py`。它创建临时账号目录，验证并发启动、实际账号创建 API、真实 AstrBot 平台加载/卸载、进程停止、重启后数据保留和令牌轮换。不登录微信或发送任何消息。

上游许可、协议和媒体组件声明保存在 `licenses/`；修改后的完整来源可以通过固定提交与补丁重建。
