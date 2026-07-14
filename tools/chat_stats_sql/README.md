# 聊天统计 SQL 工具

这个工具会在用户明确询问聊天历史里的“数量 / 次数 / 排行 / 谁最多 / 某时间段发了多少”时，让 Agent 调用 `query_chat_stats_sql`。

它和 RAG 历史检索分工不同：

- SQL 统计：回答可聚合问题，例如“某人说了多少次某词”“今天谁发言最多”。
- RAG 检索：回答历史上下文问题，例如“当时发生了什么”“帮我找那段聊天”“谁之前提过这个话题”。

## 安装

复制整个目录到 bot 数据目录：

```bash
cp -r tools/chat_stats_sql data/nonebot_plugin_groupmate_agent/tools/
```

部署后结构：

```text
data/nonebot_plugin_groupmate_agent/tools/
└── chat_stats_sql/
    └── __init__.py
```

## 配置

这个工具没有额外环境变量。

它依赖主插件已有的：

- 聊天历史数据库
- `nonebot-plugin-orm`
- `sqlalchemy`

## 依赖

额外依赖见 `requirements.txt`。

如果你使用的是新版 `nonebot-plugin-groupmate-agent`，这些依赖通常已经由主插件安装。

## 使用方式

用户在群里提出类似统计问题时，Agent 会自动决定是否调用：

- 我说了多少次草
- xx 说了多少次猫
- 今天谁发言最多
- 这个月谁最爱说晚安
- 我最近 7 天发了多少张图

工具默认只查询当前会话内的聊天历史，不会跨群统计。它不会返回大段原始聊天内容。

工具返回的是 JSON 事实包，不是最终群聊回复。Agent 应根据结果自己组织自然语言：

- “说了多少次 / 出现多少次”默认使用 `occurrence_count`。
- `message_count` 只表示有多少条文本消息包含关键词；除非用户明确问“多少条消息包含 xx”，否则不要主动提。
- 排行类结果读取 `rows`。
- 回复里不要提数据库、SQL、RAG、JSON、字段名或工具名。
