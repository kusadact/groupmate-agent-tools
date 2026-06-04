# 语音工具

这个工具会在用户明确要求“发语音 / 用语音说 / 念出来 / 读出来”时，让 Agent 调用 `send_voice`。

它会把短文本发送给 GPT-SoVITS 类 TTS 服务，然后把返回的音频作为语音消息发送到当前群聊。

## 安装

复制整个目录到 bot 数据目录：

```bash
cp -r tools/voice data/nonebot_plugin_ai_groupmate/tools/
```

部署后结构：

```text
data/nonebot_plugin_ai_groupmate/tools/
└── voice/
    └── __init__.py
```

## 配置

把 `env.example` 里的配置复制到你的 bot `.env`，然后填写实际地址：

```dotenv
ai_groupmate__voice_enabled=true
ai_groupmate__voice_base_url=http://127.0.0.1:9880
ai_groupmate__voice_text_lang=zh
ai_groupmate__voice_speed_factor=1.0
ai_groupmate__voice_top_k=15
ai_groupmate__voice_top_p=1.0
ai_groupmate__voice_temperature=1.0
```

`voice_base_url` 应指向你的 TTS 服务根地址。工具默认使用：

- 健康检查：`GET /`
- 合成接口：`POST /tts`

健康检查失败时，主插件不会把语音工具注入 Agent prompt。

## 依赖

额外依赖见 `requirements.txt`。

如果你使用的是新版 `nonebot-plugin-ai-groupmate`，`httpx` 通常已经由主插件安装。

## 使用方式

用户明确要求语音时，Agent 会自动决定是否调用，例如：

- 用语音说一句
- 念出来
- 发条语音

工具会限制单次语音文本长度，避免生成过长语音。
工具声明了 `ToolLimitSpec(tool_name="send_voice", run_limit=1)`，每轮 Agent 最多发送一次语音；如果当前请求已过期，工具会取消发送。
