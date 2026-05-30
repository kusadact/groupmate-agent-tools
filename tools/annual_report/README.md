# 年度报告工具

这个工具会在用户请求“年度报告 / 个人总结 / 成分分析”时，让 Agent 调用 `generate_and_send_annual_report`。

它会读取当前群聊里的聊天历史，生成包含发言统计、活跃时间、热词、群内排行和关系回顾的年度报告。

## 安装

复制整个目录到 bot 数据目录：

```bash
cp -r tools/annual_report data/nonebot_plugin_ai_groupmate/tools/
```

部署后结构：

```text
data/nonebot_plugin_ai_groupmate/tools/
└── annual_report/
    └── __init__.py
```

## 配置

这个工具没有额外环境变量。

它依赖主插件已有的：

- 聊天历史数据库
- 用户关系数据
- 当前 Agent 使用的 LLM

## 依赖

额外依赖见 `requirements.txt`。

如果你使用的是新版 `nonebot-plugin-ai-groupmate`，这些依赖通常已经由主插件安装。

## 使用方式

用户在群里提出类似请求时，Agent 会自动决定是否调用：

- 生成我的年度报告
- 来个个人总结
- 分析一下我的成分

工具生成完成后会直接发送报告文本。
