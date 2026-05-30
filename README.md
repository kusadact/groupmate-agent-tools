# AI Groupmate Tools

这里存放 `nonebot-plugin-ai-groupmate` 的用户自定义 Agent 工具。

这些工具不会随主插件一起发布，需要手动放到机器人的数据目录：

```text
data/nonebot_plugin_ai_groupmate/tools/
```

## 目录结构

每个工具一个文件夹，工具入口使用 `__init__.py`：

```text
tools/
├── annual_report/
│   ├── __init__.py
│   ├── README.md
│   ├── env.example
│   └── requirements.txt
└── voice/
    ├── __init__.py
    ├── README.md
    ├── env.example
    └── requirements.txt
```

## 安装方式

把需要的工具目录复制到机器人的工具目录：

```bash
mkdir -p data/nonebot_plugin_ai_groupmate/tools
cp -r tools/annual_report data/nonebot_plugin_ai_groupmate/tools/
cp -r tools/voice data/nonebot_plugin_ai_groupmate/tools/
```

最终结构应类似：

```text
data/nonebot_plugin_ai_groupmate/tools/
├── annual_report/
│   └── __init__.py
└── voice/
    └── __init__.py
```

然后按每个工具目录里的 `README.md` 配置环境变量和额外依赖。

## 工具规范

每个工具模块至少需要提供 `build(ctx)`：

```python
async def build(ctx):
    ...
```

如果工具需要检查外部服务、配置项或依赖，也可以提供可选的 `healthcheck(ctx)`：

```python
async def healthcheck(ctx):
    return True, "ok"
```

当健康检查失败时，主插件不会把这个工具注入 Agent prompt。

## 已包含工具

- `annual_report`：根据当前群聊历史生成用户年度报告。
- `voice`：调用 GPT-SoVITS 类服务，将短文本合成为语音并发送。

## 注意事项

- 不要把真实 API Key、私有服务地址、群聊数据提交到公开仓库。
- `env.example` 只提供配置示例，实际值应写到你的 bot `.env`。
- 如果工具依赖额外 Python 包，请参考对应工具目录里的 `requirements.txt`。
