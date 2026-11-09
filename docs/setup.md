# 微信消息适配器 1.1.0：安装集成版

本版只支持 Linux x86_64（amd64），Python 3.12+、glibc 2.36+。推荐官方 AstrBot v4.29.4 Docker 镜像。Windows、ARM64、32 位 x86、Alpine/musl 不在本版支持范围。

## 安装

1. 如已使用独立 Native，先停用旧的微信机器人和旧 Native 容器，保留它们的数据卷。
2. 在插件管理中通过 `https://github.com/qsc20001102/astrbot_plugin_webridge` 安装，或压缩本插件目录安装，也可放到 AstrBot 的 `data/plugins/` 下。避免多套一层同名文件夹。
3. 确认目录中存在 `bin/linux-x86_64/wx` 和 `native/manifest.json`；仓库和发行包均已包含引擎，从 Git 安装也无须先构建。
4. 重启 AstrBot，刷新管理页面，进入“插件管理 → 微信消息适配器 → 可视化视图 → 微信管理”。
5. 点击“添加微信”，填写中文或英文名称，扫描二维码，在手机微信上确认登录。
6. 点击“一键接入 AstrBot”，消息平台自动创建并启用。
7. 到“机器人”页面给新平台选择配置文件，设置模型、唤醒、插件权限和会话规则。

不再填写 `server_url`、`token` 或 `token_file`。需要手动创建时，平台类别选“微信消息适配器”，只填账号 ID；仅有一个已登录账号时可以留空。不要为同一账号同时启用多个机器人。

## Linux Docker 网络

Native 登录的部分手机确认流程需要手机从局域网访问引擎的回调端口 8050～8100。Linux 建议使用宿主机网络；只把原来的 Native 合并进 AstrBot，不需要额外的 Native 服务。

下面是**新部署**的最小示例；已有 AstrBot 部署应保留原有数据挂载和其他配置，仅按需调整网络。

```yaml
services:
  astrbot:
    image: soulter/astrbot:v4.29.4
    container_name: astrbot
    restart: unless-stopped
    network_mode: host
    stop_grace_period: 30s
    volumes:
      - ./data:/AstrBot/data
```

宿主机网络模式下不用再写 `ports` 映射。管理界面的默认端口仍为 6185；手机与服务器应能互通，局域网防火墙允许必要的登录回调。不需要暴露 Native 内部 API；该 API 只绑定 `127.0.0.1` 随机端口。

扫码二维码在界面自动刷新；扫码后按手机提示确认。取消后可重新获取。页面显示“已登录”后再接入 AstrBot。登录卡住时，先检查手机与服务器的网络互通情况，不要反复创建新账号。

## 新数据

| 数据 | 目录/文件 |
| --- | --- |
| 独立登录与 Native 状态 | `data/plugin_data/astrbot_plugin_webridge/native/` |
| 中文账号名称 | 同级 `account-labels.json` |
| 游标、去重、撤回、发送回执 | 同级独立 SQLite 状态库 |
| 消息平台绑定 | AstrBot 主配置的 `platform` 列表 |

首次集成从新账号和会话开始，不导入旧插件或旧 Native Docker 数据；后续重启保留微信消息适配器自己的登录和收发状态。退出登录保留账号文件，卸载插件也不会清空该目录。

## 安装后验收

先确认“微信管理”的引擎、账号状态和一键接入结果，再测试私聊与群聊、指令参数、图片“查看原图”、视频发送和语音，并确认收到视频时直接忽略。图片仍是图片消息，JPEG 原图不重新压缩，PNG 等转为完整尺寸 JPEG。最终手机显示和账号网络连通性需在实际部署中验证。
