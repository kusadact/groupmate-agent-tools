# 找男娘工具

这个工具会在用户明确要求“找男娘 / 抓男娘 / 谁是男娘 / 群里的男娘”等群聊整活时，让 Agent 调用 `find_femboy_in_recent_chat`。

它会从主插件传入的最近 20 条聊天记录里，筛选非 bot 发言者，然后纯随机抽一个人，实际艾特这个人，并发送一条明确指认“群里的男娘就是：某某”的玩笑文案。

抽中目标后，工具会只读取主插件当前用户画像表 `nonebot_plugin_groupmate_agent_userrelation` 中的标签和关系状态，把它们作为编理由素材。标签不会参与抽签，也不会改变随机概率。

“男娘”在这个工具里只作为群聊玩笑标签处理，不作真实身份、性别、性取向或性别认同判断。工具会尽量把输出写成明显的随机整活和胡编理由。

## 安装

复制整个目录到 bot 数据目录：

```bash
cp -r tools/find_femboy data/nonebot_plugin_groupmate_agent/tools/
```

部署后结构：

```text
data/nonebot_plugin_groupmate_agent/tools/
└── find_femboy/
    └── __init__.py
```

## 配置

这个工具没有额外环境变量。

它依赖主插件已有的：

- 最近聊天记录上下文 `ctx.history`
- 当前 Agent 使用的 LLM
- 用户画像表 `nonebot_plugin_groupmate_agent_userrelation`
- 可选的聊天 RAG 数据库

如果用户画像不存在、RAG 未启用或检索失败，工具会自动降级为只使用最近 20 条聊天记录。

## 使用方式

用户在群里提出类似请求时，Agent 会自动决定是否调用：

- 找男娘
- 今天谁是男娘
- 抓一个男娘出来
- 群里的男娘是谁

工具会直接发送结果。调用成功后，Agent 不需要复述工具输出。
