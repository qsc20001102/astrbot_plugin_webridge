# 微信消息适配器

<img src="logo.png" width="88" height="88" alt="微信消息适配器图标" />

微信消息适配器将个人微信接入 AstrBot，内置 Native 引擎，在插件界面扫码登录并一键创建消息平台。微信管理使用账号列表与登录、接入工作区，支持浅色、深色及手机布局；旧的 connection 页面及接口已移除。只负责消息收发，唤醒、白名单、模型和业务插件使用 AstrBot 配置。

**版本 1.1.2 · AstrBot v4.29.4 · Linux x86_64（amd64）· Python 3.12+ · glibc 2.36+**。

独立维护仓库：[qsc20001102/astrbot_plugin_webridge](https://github.com/qsc20001102/astrbot_plugin_webridge)。仓库自带运行所需引擎，可以直接安装仓库或压缩本目录安装。

## 安装与登录

1. 在 AstrBot 插件管理中填写本仓库地址安装；也可以将本目录压缩安装，或放到 `data/plugins/astrbot_plugin_webridge`。目录下直接包含 `main.py`、`metadata.yaml` 和 `bin/linux-x86_64/wx`。
2. 重启 AstrBot，打开“插件管理 → 微信消息适配器 → 可视化视图 → 微信管理”。
3. 点击“添加微信”，输入账号名称，用手机微信扫码并确认。
4. 登录成功后点击“一键接入 AstrBot”。插件自动创建、启用微信消息适配器消息平台，无须填写服务地址、Token 或文件路径。
5. 在 AstrBot 的“机器人”页面为新平台选择配置文件；首次创建沿用 AstrBot 默认配置。

如果从独立 Native 的旧版微信消息适配器切换，先停用旧机器人和旧 Native 容器，再在新界面重新扫码。保留旧容器和卷中的数据；本版不会导入它们。不要让两个引擎同时控制同一登录环境。

Docker 部署建议在 Linux 使用 `network_mode: host`，有些手机确认需要手机经局域网访问登录回调端口。见[安装说明](docs/setup.md)。无需再启动单独的 Native 容器。

手动新增机器人也只需选择“微信消息适配器”并指定账号 ID；只有一个已登录账号时可留空。`id`、`type`、`enable` 是框架通用字段。账号 ID 可从插件管理界面对应机器人配置中取得，日常推荐使用一键接入。

## 图片清晰度

仍发送为**图片消息**，支持微信“查看原图”。完整包内的 Native 已修复上游 v1.5.1 原图也被压到约 64 KiB 的限制：

- JPEG 原图保留完整尺寸和原始字节，不重新压缩。
- PNG 等图片转为完整尺寸的高质量 JPEG，不缩小长图；透明背景与格式不会原样保留。
- 普通预览图和缩略图仍会压缩；需要点“查看原图”查看完整尺寸图片。
- 原图支持分块上传，逐块核对确认与完整性，保留取消、幂等和回执处理。

源图若已经由业务插件或图床缩小，微信消息适配器无法恢复细节。JPEG 字节和分块完整性已通过自动测试；**手机微信实际接收与“查看原图”效果仍需安装后验收**。详见[图片说明](docs/image-recognition.md)。

## 视频消息

用户发给机器人的视频直接忽略：不下载视频或缩略图，不交给模型和业务插件，也不自动回复。引用视频并附带文字时，仅交付文字与引用信息。

机器人仍以视频消息发送结果。取消原先的 50 MiB 来源限制、约 25 MiB 播放副本限制，以及为了满足体积而降低码率的策略。兼容的 H.264/AAC MP4 保留源字节；其他 MOV 家族编码按来源尺寸与帧率做兼容转换，不统一缩到 1080p 或 60 帧。

微信服务端、传输字段和部署资源仍有实际边界，不能承诺任意大小都能被微信接收。详见[视频说明](docs/video-quality.md)。

## AstrBot 行为配置

| 需求 | 配置位置 |
| --- | --- |
| 唤醒词和指令 | `wake_prefix`、启用的业务插件 |
| 私聊需要前缀 | `platform_settings.friend_message_needs_wake_prefix` |
| 群/会话白名单 | `platform_settings.enable_id_white_list`、`id_whitelist` |
| 群成员独立会话 | `platform_settings.unique_session` |
| 回复 @ / 引用 | `platform_settings.reply_with_mention`、`reply_with_quote` |
| @全体是否唤醒 | `platform_settings.ignore_at_all` |
| 频率限制 | `platform_settings.rate_limit` |
| 模型、人格、ASR/TTS、插件范围 | 机器人绑定的 AstrBot 配置及会话设置 |

未 @ 的群消息也会交给 AstrBot，由框架决定是否响应。适配器不伪造 @，不添加图片描述提示词，不用自己的白名单或群回复规则。

## 消息能力

- 接收和发送标准文本、图片、语音，以及文本携带的真实 @、@全体和引用；支持视频发送，收到视频直接忽略。
- 支持普通回复、`event.send` 和按保存会话主动发送。指令和参数交给框架或业务插件解析。
- 标准 `Node` / `Nodes` 发送为微信“聊天记录”合并转发卡片，支持文本、图片和嵌套节点，保留署名与时间。卡片中的 @ 仅显示为文字。
- 接收微信合并转发及其引用，保留节点文字、署名、时间与嵌套结构。内嵌图片、语音、视频、文件等目前保留类型占位符，不自动下载。发送节点中的语音、视频按原顺序拆为独立媒体消息，其他支持内容仍组成聊天记录卡片；普通文件在发送前报错。
- 图片单张最多 10 MiB；语音来源最多 26,214,383 字节、总长 300 秒。Native 负责音视频准备，视频仍受账号能力限制。
- 保留 SSE 游标、断线重连、持久化去重、撤回保护、发送幂等、异步 job 和分段语音回执；不确定结果不自动重发。
- PDF 功能已移除，普通文件收发尚未实现。纯媒体携带 @ / 引用受 Native 限制，需要非空文本承载。

依赖 QQ 专有 API 或硬编码平台类型的插件，仍需对应插件适配。

链接解析器若配置了“群组消息必须 @ 机器人”（`require_at_in_group`），微信群内直接发送链接会被解析器跳过。希望自动解析时，在该解析器配置中关闭此项，并检查其会话白名单和防抖设置。

## 渲染图片与聊天记录

兼容 AstrBot `event.image_result()`、本地文件、base64 和 HTTP(S) 图片。渲染服务可以使用自定义端口；内网渲染服务须配置在 AstrBot 的 `t2i_endpoint` 中。读取失败且尚未提交任何回复时，业务插件可以发送文本降级；已发送部分内容或回执不确定时仍保留防重发保护。

聊天记录遵循机器人所绑定配置的 `provider_settings.quoted_message_parser`：`max_component_chain_depth` 控制组件递归深度，`max_forward_node_depth` 控制嵌套记录深度。使用 AstrBot 官方解析器提供文本；不会把记录内的指令、@ 当成本条消息的唤醒条件。`max_forward_fetch` 是官方远程拉取次数限制，本适配器使用消息内嵌记录，不额外拉取远程转发内容。

1.1.1 修正聊天记录内图片显示“已被清理”的问题：使用记录消息专用字段，正文图片与缩略图独立上传并写入匹配的密钥、大小和摘要。升级时须一并更新内置引擎并重启 AstrBot，再重新发送聊天记录；旧卡片中的错误附件信息不会自动修复。

业务插件按官方约定构造 `Node(name=..., uin=..., time=..., content=[Plain(...), Image(...)])` 或 `Nodes([...])` 后调用 `event.send(event.chain_result([...]))`，主动发送也使用同样的消息链。`platform_settings.forward_threshold` 属于官方 QQ 自动折叠选项，本平台不使用它自动改变回复样式。

## 数据与运行

插件标识 `astrbot_plugin_webridge`，平台类型 `webridge`，显示名称“微信消息适配器”。本次改名沿用这些内部标识，已有账号和平台绑定可继续使用。数据保存在 `data/plugin_data/astrbot_plugin_webridge`：独立登录目录、中文账号备注及收发状态库。不读取、复制或迁移原插件的数据和会话。

内置引擎只监听本机回环地址，端口、令牌由插件管理，不写入机器人配置或显示在 UI。账号操作通过已登录的 AstrBot 插件页面完成。卸载会停止本插件的消息平台和引擎，账号数据保留。

上游基于 [neozhu/astrbot-plugin-wechat-native](https://github.com/neozhu/astrbot-plugin-wechat-native) 和 [thisnick/agent-wechat v1.5.1](https://github.com/thisnick/agent-wechat/tree/v1.5.1)。仓库和发行 ZIP 均包含 Linux x86_64 引擎，可直接安装。引擎固定提交、源码补丁、重建脚本和许可见 [native](native/README.md)。

## 开发与许可

本目录仅保留插件运行文件、使用文档、内置引擎及可重建的源码补丁。测试、开发依赖、临时文件与验证记录位于工作区的 `Test` 目录，不随插件安装。Native 的构建、Clippy 和回归测试由 `bash native/build-linux.sh` 或仓库 Actions 执行。

本项目独立维护，沿用上游 MIT 许可与署名；内置引擎的各项许可和来源见 `native/licenses/` 与 `native/README.md`。

[安装说明](docs/setup.md) · [架构](docs/architecture.md) · [运维](docs/operations.md)
