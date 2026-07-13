# Danbooru Setu

独立的 Groupmate Agent Danbooru 搜图工具。

## 做什么

- 用户明确要求“setu / 色图 / 涩图 / 搜张图 / 找张图 / 来张图”，并且同时给出了角色、作品或属性 tag 时，Agent 可以调用 `send_danbooru_setu`。
- Agent 只需要把用户原始搜索词传给工具；工具内部会单独调用当前模型解析 Danbooru canonical tag。
- 每次最多使用两个 tag。
- 工具会从 Danbooru 随机取一张匹配图片，下载后直接发送到当前群聊。
- 工具会按配置里的 `rating` 过滤图片，默认只发送 `rating:g`。
- 工具发送成功后会写入 `ChatHistory`。

## 安装

复制整个目录到 bot 数据目录：

```bash
cp -r tools/danbooru_setu data/nonebot_plugin_groupmate_agent/tools/
```

部署后结构：

```text
data/nonebot_plugin_groupmate_agent/tools/
└── danbooru_setu/
    └── __init__.py
```

要求宿主 `nonebot-plugin-groupmate-agent` 已支持用户自定义 Agent 工具目录，也就是能加载：

```text
data/nonebot_plugin_groupmate_agent/tools/<tool_name>/__init__.py
```

## 配置

把 `env.example` 里的配置复制到你的 bot `.env`：

```dotenv
groupmate_agent_danbooru_setu__name=
groupmate_agent_danbooru_setu__api=
groupmate_agent_danbooru_setu__rating=g
groupmate_agent_danbooru_setu__proxy=
```

配置说明：

- `name`：Danbooru 用户名，可空。
- `api`：Danbooru API key，可空。和 `name` 同时配置时会用于 API 请求认证。
- `rating`：Danbooru rating 过滤，默认 `g`。可填 `g`、`s`、`q`、`e`。
- `proxy`：HTTP 代理地址，可空。例如 `http://127.0.0.1:7890`。

内部固定默认值：

- Danbooru 地址：`https://danbooru.donmai.us`
- 请求超时：15 秒
- 最大 tag 数：2
- 单图发送体积：6 MiB；超过时会先尝试压缩，压缩后仍超过才取消发送
- 每轮 Agent 最多调用一次工具

## 使用方式

用户请求示例：

- `setu 初音未来`
- `搜张图 芙莉莲 白丝`
- `来张 碧蓝档案 爱丽丝`

Agent 不需要自己判断 Danbooru tag，只把用户原始搜索词放进 `raw_query`：

```text
send_danbooru_setu(raw_query="初音未来")
send_danbooru_setu(raw_query="芙莉莲 白丝")
send_danbooru_setu(raw_query="碧蓝档案 爱丽丝")
```

如果用户没有明确搜图意图，或者没有提供可搜索的角色、作品、属性，不应调用本工具。
工具会在内部解析并记录：

- 用户原始输入 `raw_query`
- 模型解析出的候选 tag
- Danbooru autocomplete 后实际请求的 tag 和 query

## 依赖

额外依赖见 `requirements.txt`。

如果使用新版 `nonebot-plugin-groupmate-agent`，`httpx`、`langchain`、`pydantic`、`nonebot-plugin-alconna`、`nonebot-plugin-orm` 通常已经由主插件安装。
