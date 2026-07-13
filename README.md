# Groupmate Agent Tools

这里存放 `nonebot-plugin-groupmate-agent` 的用户自定义 Agent 工具。

这些工具不会随主插件一起发布，需要手动放到机器人的数据目录：

```text
data/nonebot_plugin_groupmate_agent/tools/
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
├── chat_stats_sql/
│   ├── __init__.py
│   ├── README.md
│   ├── env.example
│   └── requirements.txt
├── find_femboy/
│   ├── __init__.py
│   ├── README.md
│   ├── env.example
│   └── requirements.txt
├── gpt_image_agent/
│   ├── __init__.py
│   ├── README.md
│   ├── env.example
│   └── requirements.txt
├── poke/
├── read_forward_message/
│   ├── __init__.py
│   ├── README.md
│   ├── env.example
│   └── requirements.txt
├── scheduled_tasks/
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
mkdir -p data/nonebot_plugin_groupmate_agent/tools
cp -r tools/annual_report data/nonebot_plugin_groupmate_agent/tools/
cp -r tools/chat_stats_sql data/nonebot_plugin_groupmate_agent/tools/
cp -r tools/find_femboy data/nonebot_plugin_groupmate_agent/tools/
cp -r tools/gpt_image_agent data/nonebot_plugin_groupmate_agent/tools/
cp -r tools/poke data/nonebot_plugin_groupmate_agent/tools/
cp -r tools/read_forward_message data/nonebot_plugin_groupmate_agent/tools/
cp -r tools/scheduled_tasks data/nonebot_plugin_groupmate_agent/tools/
cp -r tools/voice data/nonebot_plugin_groupmate_agent/tools/
```

最终结构应类似：

```text
data/nonebot_plugin_groupmate_agent/tools/
├── annual_report/
│   └── __init__.py
├── chat_stats_sql/
│   └── __init__.py
├── find_femboy/
│   └── __init__.py
├── gpt_image_agent/
│   └── __init__.py
├── poke/
├── read_forward_message/
│   └── __init__.py
├── scheduled_tasks/
│   └── __init__.py
└── voice/
    └── __init__.py
```

然后按每个工具目录里的 `README.md` 配置环境变量和额外依赖。

## 工具规范

主插件会从 `data/nonebot_plugin_groupmate_agent/tools` 加载 `<tool>.py` 或 `<tool>/__init__.py`；本仓库采用每个工具一个目录的形式。

每个工具模块需要提供 `build(ctx)`，可选提供 `healthcheck(ctx)`；两者都可以是同步或异步函数。`build(ctx)` 应返回 `OptionalToolBundle`：

```python
from typing import Any

from langchain.tools import ToolRuntime, tool
from nonebot_plugin_groupmate_agent.agent.optional_tools import (
    AgentSkill,
    OptionalToolBundle,
    OptionalToolContext,
    ToolLimitSpec,
)


async def healthcheck(ctx: OptionalToolContext) -> tuple[bool, str]:
    return True, "ok"


async def build(ctx: OptionalToolContext) -> OptionalToolBundle:
    @tool("my_tool")
    async def my_tool(text: str, runtime: ToolRuntime[Any]) -> str:
        """工具说明会提供给模型。"""
        # runtime 由主插件的 LangGraph 执行器注入，不需要模型填写。
        return f"{runtime.context.session_id}: {text}"

    return OptionalToolBundle(
        name="my_tool",
        tools=[my_tool],
        skills=[
            AgentSkill(
                name="my_skill",
                description="需要调用 my_tool 的任务。",
                prompt="- 需要调用 my_tool 时，优先给出明确的 text 参数",
                tool_names=("my_tool",),
            )
        ],
        tool_limits=[ToolLimitSpec(tool_name="my_tool", run_limit=1)],
    )
```

当健康检查失败时，主插件不会把这个工具和它的 Skill 注入 Agent。未加载 Skill 时，`my_tool` 不会出现在当前轮的工具 schema 中；模型应先调用主插件提供的 `load_agent_skill`。Skill 加载成功后，完整规则会作为工具结果返回，下一轮才开放 `my_tool`。如果工具函数声明了 `runtime: ToolRuntime[Any]` 参数，主插件会自动注入当前会话和请求上下文。`tool_limits` 可限制本轮调用次数，`tool_name=None` 表示调整全局工具调用上限。

长耗时工具应使用 `ctx.create_detached_task(...)` 启动后台任务，并在真正发送结果前检查 `ctx.can_continue`；发送成功后可调用 `ctx.mark_sent()` 标记本轮已有输出。

## 已包含工具

- `annual_report`：根据当前群聊历史生成用户年度报告。
- `chat_stats_sql`：使用受控 SQLAlchemy 查询统计当前群聊历史里的次数、数量和排行，和 RAG 历史语义检索分工区分。
- `find_femboy`：从最近 20 条聊天记录里的非 bot 发言者中纯随机抽一个人，结合用户画像标签和 RAG 素材生成群聊整活文案。
- `gpt_image_agent`：调用 GPT Image 类接口生成并发送图片，可消费主插件内置 QQ 头像工具返回的参考图路径。
- `poke`：让 Agent 在用户明确要求时戳一戳群友，每轮最多调用 3 次。
- `read_forward_message`：读取并总结 QQ 合并转发消息。
- `scheduled_tasks`：给 Agent 增加固定文本定时发送和到点重新进入 Agent 的预定任务能力。
- `voice`：调用 GPT-SoVITS 类服务，将短文本合成为语音并发送。

## AgentSkill 迁移

长规则和低频工具 schema 已迁移到按需 `AgentSkill`。详细的迁移范围、可见性测试和首轮 token 对比见 [AGENTSKILL_MIGRATION.md](AGENTSKILL_MIGRATION.md)。主插件依赖至少包含 `feat: add lazy agent skills for optional tools`（`3faf9e8`）或其后续版本。

## 注意事项

- 不要把真实 API Key、私有服务地址、群聊数据提交到公开仓库。
- `env.example` 只提供配置示例，实际值应写到你的 bot `.env`。
- 如果工具依赖额外 Python 包，请参考对应工具目录里的 `requirements.txt`。
