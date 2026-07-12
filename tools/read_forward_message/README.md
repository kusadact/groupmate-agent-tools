# 合并转发阅读工具

这个工具会在用户明确要求查看、阅读、总结或分析 QQ 合并转发消息时，让 Agent 调用
`read_forward_message`。

工具会先发送一条“正在查看”的等待提示，再通过 OneBot v11 的 `get_forward_msg`
API 读取合并转发内容，整理文字和图片，生成 100 字以内的简要摘要和一句简短评价并返回给 Agent。
Agent 应该只用 `reply_user` 发送一次这个摘要。

## 安装

复制整个目录到 bot 数据目录：

```bash
cp -r tools/read_forward_message data/nonebot_plugin_groupmate_agent/tools/
```

部署后结构：

```text
data/nonebot_plugin_groupmate_agent/tools/
└── read_forward_message/
    └── __init__.py
```

## 配置

这个工具没有额外环境变量。

它依赖主插件已有的：

- OneBot v11 适配器和 `bot.call_api`
- 当前 Agent 使用的 LLM
- 可选的多模态模型配置，用于查看合并转发里的图片

如果配置了主插件的多模态模型，工具会优先把合并转发中的前几张图片交给多模态模型一起总结。
如果图片不可读取或多模态总结失败，会降级为纯文本总结，并用 `[图片]` 占位。

## 依赖

额外依赖见 `requirements.txt`。

如果你使用的是新版 `nonebot-plugin-groupmate-agent`，这些依赖通常已经由主插件安装。
工具基于主插件的用户自定义 Agent 工具接口，入口函数为 `build(ctx)`，返回 `OptionalToolBundle`。

## 使用方式

用户在群里提出类似请求时，Agent 会自动决定是否调用：

- 看看这条合并转发
- 总结一下我回复的合并转发
- 读一下这个转发里都说了什么
- 分析一下这条合并转发

工具成功后会把摘要作为工具结果返回给本轮 Agent，方便本轮继续调用其他工具。
最终摘要由主插件的 `reply_user` 发送，因此会沿用主插件原有的发送、去重和聊天历史入库逻辑。

## 限制

- 除非用户主动请求查看合并转发，否则 prompt 要求 Agent 不调用本工具。
- 一轮最多调用一次。
- 默认最多读取 100 个合并转发节点。
- 摘要和评价合计控制在 100 字以内。
- 嵌套合并转发不展开，显示为 `[合并转发]`。
- 语音、视频、文件等非文本内容会用占位文本表示。
- 如果用户没有回复合并转发，工具会优先使用主插件提供的近期合并转发缓存；
  没有缓存时才尝试从最近已入库消息 id 回查 `get_msg`。
