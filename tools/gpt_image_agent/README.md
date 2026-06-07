# GPT Image Agent

独立的 Groupmate Agent 生图工具。

## 做什么

- 用户明确要求“生成图片 / 画图 / 做图 / P图 / 改图 / 编辑图片 / 重绘 / 合成 / 给某人头像做风格化二创”时，Agent 可以调用 `generate_and_send_image`。
- 用户只是说“发一张图片 / 来张图 / 给我一张图片 / 发图 / 找张图片 / 看图”时，不应调用本工具；这类表达只表示想收到图片，不等于要求 AI 图片模型生成或修改。
- 如果用户只是要求发送或查看原始 QQ 头像，应由主插件内置 `send_qq_avatar_image` 处理，不应调用本工具。
- 如果用户说“给 XX 用户头像加墨镜 / 用 XX 头像做赛博头像”，由主插件内置的 QQ 头像工具负责取头像。
- 内置 `fetch_qq_avatar_references` 会下载头像并返回本地图片路径。
- 头像路径随后传给 `generate_and_send_image.reference_image_paths`。
- 有头像参考图时走编辑接口；没有参考图时走纯文本生成接口。
- 生成成功后直接发送图片到当前群聊，并写入 `ChatHistory`。

## Agent 工具

- `generate_and_send_image`：生成并发送图片；需要头像参考图时接收 `reference_image_paths`。
- QQ 头像匹配和下载已经拆到主插件内置系统工具。

## 安装

复制整个目录到 bot 数据目录：

```bash
cp -r tools/gpt_image_agent data/nonebot_plugin_groupmate_agent/tools/
```

部署后结构：

```text
data/nonebot_plugin_groupmate_agent/tools/
└── gpt_image_agent/
    └── __init__.py
```

要求宿主 `nonebot-plugin-groupmate-agent` 已支持用户自定义 Agent 工具目录，也就是能加载：

```text
data/nonebot_plugin_groupmate_agent/tools/<tool_name>/__init__.py
```

## 配置

复制 `env.example` 到 bot 的 `.env`。

主 `.env` 只需要暴露这两个：

```dotenv
groupmate_agent_image_agent__base_url=https://your-relay.example/v1
groupmate_agent_image_agent__api_key=sk-xxxxxx
```

其它都有默认值：

```text
model=gpt-image-2
size=1024x1024
quality=auto
generation_endpoint=/images/generations
edit_endpoint=/images/edits
edit_image_field_name=auto
timeout_seconds=180
download_timeout_seconds=30
retry_attempts=2
retry_delay_seconds=5
max_prompt_length=1200
max_reference_avatars=2
enabled=true
```

如果以后你的中转接口图片字段不是 `image[]` 或 `image`，再额外加：

```dotenv
groupmate_agent_image_agent__edit_image_field_name=your_field_name
```

默认 `auto` 会先试 `image[]`，失败后试 `image`。
