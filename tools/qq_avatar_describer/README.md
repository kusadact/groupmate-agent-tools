# QQ Avatar Describer

让 Agent 能“看”QQ 头像并返回文字描述的工具。

## 做什么

- 用户问“某人的头像长什么样 / 描述一下头像 / 看看头像内容”时，Agent 先调用主插件内置 `fetch_qq_avatar_references`。
- `fetch_qq_avatar_references` 负责匹配群成员或 QQ 号，并下载头像到本地。
- 本工具接收返回里的 `path=...`，把本地头像图片喂给主插件已配置的多模态模型。
- 工具只返回文字描述，不会发送图片，也不会生成或修改图片。

如果用户只是要把原头像发到群里，应该调用主插件内置 `send_qq_avatar_image`。
如果用户要用头像做图、P 图、二创，应该把 `fetch_qq_avatar_references` 的 `path` 交给 `gpt_image_agent`。

## Agent 工具

- `describe_qq_avatar_image`：查看 QQ 头像参考图并返回内容描述。

## 安装

复制整个目录到 bot 数据目录：

```bash
cp -r tools/qq_avatar_describer data/nonebot_plugin_ai_groupmate/tools/
```

部署后结构：

```text
data/nonebot_plugin_ai_groupmate/tools/
└── qq_avatar_describer/
    └── __init__.py
```

## 配置

本工具不单独配置 QQ 头像尺寸、缓存或下载参数。

它复用主插件的多模态配置：

```dotenv
ai_groupmate__multimodal_model=qwen-vl-max
ai_groupmate__multimodal_base_url=https://dashscope.aliyuncs.com/compatible-mode/v1
ai_groupmate__multimodal_api_key=sk-xxxxxx
```

如果没有单独配置 `ai_groupmate__multimodal_api_key`，主插件会回退使用 `ai_groupmate__qwen_key`。
