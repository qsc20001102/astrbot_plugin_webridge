# 内置微信引擎

发行包内含 `bin/linux-x86_64/wx`，由插件启动和停止，不需要单独的 Native Docker 容器。

| 项目 | 固定版本 |
| --- | --- |
| 上游 | [thisnick/agent-wechat v1.5.1](https://github.com/thisnick/agent-wechat/tree/v1.5.1) |
| 提交 | `39af1eec84f4763e1257406f8c5fb848b31d82e9` |
| Rust | 1.93.1，默认 Cargo 功能 |
| 构建基础 | 官方 `rust:1.93.1-bookworm`，Dockerfile 固定镜像摘要 |
| 运行目标 | Linux x86_64，glibc 2.36+ |
| 补丁（按顺序） | `patches/original-image.patch`、`patches/merged-forward.patch` |
| 二进制与补丁校验 | `manifest.json` |

媒体补丁保留 JPEG 原始字节；PNG 等图片生成完整尺寸的高质量 JPEG。预览图、缩略图仍分别准备，原图沿用已有 CDN 上传结构分块提交，并检查每块确认、文件 ID、完整长度和取消状态。旧任务的图片准备方式保留，避免改变旧幂等任务的结果。没有添加未经服务端支持的 API 参数。

视频播放副本会优先保留兼容的 H.264/AAC MP4；其他来源按源尺寸和帧率转换编码；取消 50 MiB/约 25 MiB 策略限制和按体积压低码率的处理，收到视频和缩略图不自动下载，详细条件见 [视频说明](../docs/video-quality.md)。清单的 `video_passthrough`、`video_size_policy` 和 `incoming_video_download` 标记与二进制 SHA-256 一起验证。

源码补丁包含合成图像回归测试：长图、50 MP JPEG、PNG 转换、完整原图分块重组/解密、预览只发送一次和中途取消。测试不等同于手机微信接收验收。

合并转发补丁在现有 `/sends`、持久化 app-message 队列及回执机制上增加 `kind: forward`，构造微信 subtype 19 聊天记录。支持文字、图片 CDN 描述符与嵌套记录，保留节点署名与时间。接收及引用投影提供原始记录字段，由适配器按 AstrBot 的当前配置解析，不沿用引擎固定深度的显示预览。

聊天记录图片使用记录消息字段 `cdndataurl`、`cdnthumburl`、`datasize`、`thumbsize`。正文与缩略图分别按原图附件上传，使用独立文件 ID 和 AES 密钥，同时写入完整 MD5、前 256 字节 MD5、缩略图尺寸。不能套用收藏 XML 的字段名，也不能复用普通聊天图片的同一文件 ID 加不同质量参数。回归测试按记录消息的读取方式选择附件并解密，核对实际字节、大小和摘要。

## 重建

在 Linux x86_64 安装 Docker、Git、Python 3，然后从插件根目录执行：

```bash
bash native/build-linux.sh
```

脚本下载固定的公开源码，按顺序应用补丁，执行格式、全工作区 Clippy 和相关测试，再编译、去除调试符号并更新清单。构建目录默认位于相邻 `Test/webridge-native-build`，也可通过 `WEBRIDGE_BUILD_DIR` 指定，不接触微信账号和现有容器数据。仓库 Actions 使用同一脚本。

Linux x86_64 二进制已纳入 Git，仓库和完整安装 ZIP 均可直接安装。需要重建时使用上述脚本；引擎与补丁的 SHA-256 必须和清单一致。检出上游源码时应关闭换行自动转换，尤其不要将 Native 的快照测试夹具转换为 CRLF。

## 验证

Python 回归、扫码页面浏览器测试、隔离环境启动与登录检查脚本均放在工作区 `Test`，不随插件安装。构建脚本执行锁定引擎的 Rust 回归；合并转发覆盖 XML 转义、署名、时间、嵌套、图片描述符和 protobuf app subtype。手机端实际收件效果须通过真实账号验收。

上游许可、协议和媒体组件声明保存在 `licenses/`；修改后的完整来源可以通过固定提交与补丁重建。
