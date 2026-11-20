# 视频发送为什么慢，以及本版的画质优先策略

## 结论

慢不在 AstrBot，也不在“微信服务端处理几秒的视频”，而在插件交给内置 Native
引擎之后的三个串行步骤：

1. **全分辨率重编码**。引擎对播放副本执行 `ffmpeg`：H.264、CRF 18、保持来源
   分辨率和帧率，只给 x264 两个线程。手机常见的 HEVC、1080p60、4K60 都会走
   这条路。
2. **64 KiB 顺序分块上传**。播放副本要按 64 KiB 一块、一块接一块地 POST 到微信
   CDN，没有并发。43 MiB 的视频就是约 660 次串行请求。
3. **账号锁 + 插件发送锁**。引擎在整个“转码 + 上传”期间持有账号锁；本插件的
   `_send_lock` 又会让同一账号的其它消息排队。前一条媒体任务没结束时，后一条
   视频只能等。

三步叠加，一条几十秒的手机视频在低配 VPS 上就可能从几分钟拖到接近一小时的
发送超时；期间机器人对同一账号的其它回复也会一起卡住。

## 实测数据（速度档，显式开启）

默认的画质优先模式不转码，因此没有“本地转码耗时”这组数字。下表是
`transcode=true` + `max_edge=1280` + `max_fps=30` + `crf=23` 的速度档实测，
只比较视频转码（不含上传）：

| 来源 | 引擎式 CRF 18 全分辨率、2 线程 | 快速通道 720p30 veryfast CRF 23 |
| --- | --- | --- |
| 4K60 HEVC | **79.5 s**，输出 43.2 MiB | **6.6 s**，输出 1.2 MiB |
| 1080p60 H.264 | **17.8 s**，输出 18.1 MiB | **2.8 s**，输出 1.7 MiB |

按 64 KiB 分块折算，43.2 MiB 是约 660 次串行 CDN 请求，1.2 MiB 只要约 19 次。
即使不考虑转码，上传环节也相差一个数量级。

同分辨率/帧率 CRF 18 的可选本地转码（`transcode=true` 的默认参数）在同一素材上
约 **33 秒**、输出 36 MiB；不缩放、不降帧，但仍是 H.264 有损编码。它相当于把
引擎本来也要做的那一次编码放到本机、用更多线程和更快预设完成。是否启用取决于
你是否接受这一次编码。

> 上述是本机（Windows，较强 CPU）的数字。引擎式一列用 `-preset fast` 近似发行
> 二进制的参数（实际预设未公开，若更慢则差距更大）。部署机核心数更少、主频更低
> 时，x264 4K CRF 18 的差距会更大；跨地域访问微信 CDN 时，660 次串行请求的
> 往返延迟也会被放大。

## 官方 weixin_oc 是怎么做的

AstrBot 官方的个人微信适配器（`weixin_oc`）走的是腾讯 openclaw / iLink 接口，
视频路径非常直接：

- 解析出本地文件后调用 `ilink/bot/getuploadurl` 拿上传地址；
- 把**整个文件**做 AES-ECB 加密，然后**一次 POST** 到 CDN，读取
  `x-encrypted-param` 作为下载凭据；
- 发送 `video_item`，只带 `media` 描述符和 `video_size`。

它**不转码、不分块、不等待微信客户端回执**。相关实现：

- [weixin_oc_adapter.py](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/platform/sources/weixin_oc/weixin_oc_adapter.py)
  的 `_prepare_media_item` / `_send_media_segment`
- [weixin_oc_client.py](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/platform/sources/weixin_oc/weixin_oc_client.py)
  的 `upload_to_cdn`
- [个人微信接入文档](https://docs.astrbot.app/platform/weixin_oc.html)

需要说明：`weixin_oc` 和本插件不是同一条传输链路。本插件用 Native 模拟微信
私有协议，必须在本地准备微信可播放的 H.264 副本；官方适配器由 openclaw 服务端
承担媒体处理。因此不能直接把官方的“一次 POST”照搬过来，但“**不重复转码、尽量
一次传完**”这两个原则可以借鉴。

## 本版做了什么

新增 `video.py`，默认策略是**只透传、不重编码**：

1. 默认 `transcode=false`：Python 不做任何转码，源文件字节原样交给 Native。
   已兼容的 MP4 由引擎自己走字节保留路径；不兼容的格式（HEVC、MOV 等）由引擎
   按它原有的 CRF 18、来源分辨率流程处理。**画质与旧版一致，不会因为本插件下降。**
2. `transcode=true` 是显式选项。它的默认参数是**保持来源分辨率、帧率、声道数**，
   H.264 Main、CRF 18、veryfast；只用于把 HEVC/其他容器在更快的机器上转成兼容
   格式，不缩放、不降帧。它仍是一次有损编码，但和引擎自身的 CRF 18 同档。
3. 速度档（会缩到 1280 长边、30 fps、CRF 23）必须显式配置；开启即代表接受
   画质取舍，默认不启用。
4. 转码失败、超时、找不到 ffmpeg、输入过大时，原样交回 Native。
5. 日志增加上传耗时、任务状态/阶段、已完成字节、总耗时；等待超过 60 秒会告警；
   发送队列等待也会记录。轮询从 0.25 秒逐步退避到 2 秒，能力查询缓存 5 分钟。

默认模式下没有任何画质变化，但也不会有本地转码带来的加速；HEVC/4K 仍受
Native 自身的转码和 64 KiB 串行上传限制。

> **默认模式不会让视频变快。** 它只保证本插件不改变画质。要提速，只能在
> “同分辨率/帧率 CRF 18 本地转码”“1280 长边/30 fps 速度档”或“改 Native 上传”
> 三者中选一个；前两个都至少包含一次重编码，第三个才可能既快又不缩画质。

## 配置

推荐在 AstrBot WebUI 里改：

**插件管理 → 微信消息适配器 → 配置 → 视频发送**

AstrBot 会把值保存在：

```text
data/config/astrbot_plugin_webridge_config.json
```

首次加载插件时会按 `_conf_schema.json` 自动生成，不需要手动建文件。结构是：

```json
{
  "video": {
    "enabled": true,
    "transcode": false,
    "max_edge": 0,
    "max_fps": 0,
    "crf": 18,
    "preset": "veryfast",
    "audio_kbps": 128,
    "timeout": 120,
    "max_seconds": 3600,
    "max_output_mb": 0,
    "max_passthrough_mb": 4096,
    "max_input_mb": 512
  }
}
```

旧的 `data/plugin_data/astrbot_plugin_webridge/video_settings.json` 仍然兼容，
但**插件配置优先**：两者同时存在时，以 WebUI 保存的值为准。环境变量优先级最高。
保存后 AstrBot 会重载插件；如果日志里的“视频快速通道”没有变化，完整重启一次
AstrBot。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | `false` 完全关闭视频处理层，退回旧行为 |
| `transcode` | `false` | **默认不转码**；`true` 才允许本地重编码 |
| `max_edge` | `0` | 长边上限；`0` 保持来源分辨率（不缩放） |
| `max_fps` | `0` | 帧率上限；`0` 保持来源帧率（不降帧） |
| `crf` | `18` | 越小越清晰、文件越大；18 与引擎自身转码同档 |
| `preset` | `veryfast` | x264 预设；`superfast` 更快、`fast` 更清晰 |
| `audio_kbps` | `128` | AAC 码率；声道数跟随来源（多声道降为立体声） |
| `timeout` | `120` | 基础转码超时（秒）；长视频按 3 倍时长放宽，最多 900 秒 |
| `max_seconds` | `3600` | 超过此时长的来源不做透传判断；与引擎上限一致 |
| `max_output_mb` | `0` | 转码输出上限；`0` 表示不限制（不牺牲画质） |
| `max_passthrough_mb` | `4096` | 透传体积上限；`0` 表示不限制 |
| `max_input_mb` | `512` | 超过此大小的输入不尝试本地转码 |

同样的键可以用环境变量覆盖（优先级更高）：

`WEBRIDGE_VIDEO_FASTPATH`、`WEBRIDGE_VIDEO_TRANSCODE`、`WEBRIDGE_VIDEO_MAX_EDGE`、
`WEBRIDGE_VIDEO_MAX_FPS`、`WEBRIDGE_VIDEO_CRF`、`WEBRIDGE_VIDEO_PRESET`、
`WEBRIDGE_VIDEO_AUDIO_KBPS`、`WEBRIDGE_VIDEO_TIMEOUT`、
`WEBRIDGE_VIDEO_MAX_SECONDS`、`WEBRIDGE_VIDEO_MAX_OUTPUT_MB`、
`WEBRIDGE_VIDEO_PASSTHROUGH_MB`、`WEBRIDGE_VIDEO_MAX_INPUT_MB`。

发送等待时间也可以调：

- `WEBRIDGE_VIDEO_SEND_TIMEOUT`：默认 `3600` 秒，超时后不再等待 Native 回执。
- `WEBRIDGE_VIDEO_DOWNLOAD_TIMEOUT`：默认 `180` 秒，外部视频 URL 的读取预算。

### 推荐的几组取值

| 目标 | 配置 |
| --- | --- |
| 画质优先（默认） | `transcode=false`，Python 不碰视频字节 |
| 不缩画质的转码（HEVC/其他容器） | `transcode=true`、`max_edge=0`、`max_fps=0`、`crf=18` |
| 速度优先（明确接受画质取舍） | `transcode=true`、`max_edge=1280`、`max_fps=30`、`crf=23`、`max_output_mb=24` |
| 完全旧行为 | `enabled=false` |

`ffmpeg` / `ffprobe` 不需要额外安装：Native 引擎发视频本来就依赖它们，找不到时
快速通道会自动禁用并在日志里说明。快速通道运行在 AstrBot 进程所在主机；如果手工
配置了外部 Native，而 AstrBot 主机没有 ffmpeg，则不会启用（发送行为与旧版相同）。

## 怎么确认生效

启动日志应出现（默认）：

```text
微信消息适配器 视频快速通道：passthrough-only (no local re-encode)
```

如果显式开启本地转码，则显示：

```text
微信消息适配器 视频快速通道：transcode edge=source fps=source crf=18 preset=veryfast audio=128k cap=none timeout=120s
```

默认模式发一条视频后按顺序应看到：

```text
微信消息适配器 视频开始上传到 Native：bytes=... timeout=...
微信消息适配器 视频已上传到 Native：source_id=... elapsed=...
微信消息适配器 视频任务进度：job=... state=running phase=... progress=.../...
微信消息适配器 视频发送完成：job=... server_message=... elapsed=...
```

开启 `transcode=true` 时，前面会多出：

```text
微信消息适配器 视频快速通道：开始转码 ... 或 视频快速通道：源文件已兼容，直接复用
微信消息适配器 视频快速通道：完成 6.6s，14.5 MiB -> 1.2 MiB (8%)
```

如果日志一直停在 `state=queued`，说明不是转码慢，而是前一条媒体任务还占着
Native 的账号锁；同一账号的其它发送也会因此排队。这时先看有没有卡住的上一条
视频/语音任务。

## accepted 但本地投影 pending（已修复）

如果日志出现：

```text
state=accepted phase=completed progress=-/- elapsed=22s
```

之后仍然每 15 秒重复同一行，说明引擎**服务端已经受理**，但本地投影
（`profileProjection`）没有生成 `messageIds`。旧逻辑要求 `messageIds` 才返回，
于是一条已经送达的视频会继续占住发送锁，最长到一小时的发送超时；后面的视频
全部在“发送队列等待”里排队。这正是“16 MB 视频却几小时后才收到”的典型原因。

现在的处理：

- 服务端已受理且 `serverMessageId` 存在时，即使 `profile=pending` /
  `not_applicable`、`messageIds` 为空，也按成功返回并记录警告，不再占锁；
- `accepted` 后 15 秒仍没有 `serverMessageId`，标记待人工检查并释放发送锁；
- 进度日志增加 `profile=`、`server_id=`、`ids=`，一眼能看出卡在哪个字段。

修复后同样场景应在 `state=accepted` 的第一次轮询就结束，而不是等到 60 秒告警
甚至一小时超时。

## 仍然存在的边界

本版只优化“送进引擎之前”的输入，没有改 Native 二进制：

- 引擎内部仍是 64 KiB 串行上传。要彻底解决，需要在
  `wechat-protocol/src/media.rs` 的 `upload_video_with_transport` 里改成一次性
  POST 或多路并发分块，然后重建引擎。
- 引擎仍按账号串行处理媒体任务；要并行化，需要把 `run_media_worker` 的账号锁
  缩小到“提交”阶段，或允许同一账号的多个媒体任务并发。
- 默认模式仍把整段视频读进 Python 内存一次（上传需要字节）。`transcode=true`
  且来源是本地文件时，走路径模式：ffmpeg 直接从文件读，Python 只读取转码后的
  成片，内存峰值从“源大小”降到“成片大小”。外部 URL 仍会整段读入内存。
- 画质优先模式不限制成片大小；超长/超大视频仍受引擎的 25 MiB 级上传字段和
  64 KiB 分块数限制，发送可能很慢或失败，这是引擎边界，不是本插件能消除的。

另外，仓库里的 `native/patches` **没有包含**当前二进制实际使用的视频策略补丁：
上游 v1.5.1 的 `media_video.rs` 是 720p/30 + 按体积压码率，而发布二进制里是
`pad` + CRF 18 + `sourcePreserved`。也就是说，`native/build-linux.sh` 目前
**无法重建出与发行二进制一致的视频行为**，建议把该补丁补进仓库，避免后续维护
时回退。
