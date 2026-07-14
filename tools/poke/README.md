# Poke 工具

这个工具会在用户明确要求“戳一下 / poke / 拍一拍某个群友”时，让 Agent 调用 `poke_user`。

## 安装

```bash
cp -r tools/poke data/nonebot_plugin_groupmate_agent/tools/
```

最终结构：

```text
data/nonebot_plugin_groupmate_agent/tools/
└── poke/
    └── __init__.py
```

## 配置

不需要额外配置项。

## 使用方式

Agent 会自动判断是否调用，例如：

- “戳一下我”
- “poke 张三”
- “拍一拍 123456”

工具会尝试用 OneBot v11 常见 poke API 发送动作；如果 API 名不兼容，会退回到 poke 消息段发送。每轮 Agent 最多调用 3 次 `poke_user`。
