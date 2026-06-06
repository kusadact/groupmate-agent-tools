# 定时任务工具

这个工具给 `nonebot-plugin-ai-groupmate` 的 Agent 增加预定任务能力。

它参考了上游最新提交里内置的定时任务实现，提供两个工具：

- `schedule_message`：到点后发送一条固定文本。
- `schedule_agent_task`：到点后重新进入 Agent，让 bot 按当时上下文调用工具完成任务。

## 安装

复制整个目录到 bot 数据目录：

```bash
cp -r tools/scheduled_tasks data/nonebot_plugin_ai_groupmate/tools/
```

部署后结构：

```text
data/nonebot_plugin_ai_groupmate/tools/
└── scheduled_tasks/
    └── __init__.py
```

## 依赖

这个工具需要宿主环境已安装并加载：

- `nonebot-plugin-apscheduler`
- `nonebot-plugin-alconna`
- `nonebot-plugin-orm`

新版 `nonebot-plugin-ai-groupmate` 通常已经依赖 `nonebot-plugin-apscheduler`。如果你的 bot 环境缺少它，请安装 `requirements.txt` 里的依赖。

## 配置

工具默认启用。可选配置见 `env.example`：

```dotenv
ai_groupmate_scheduled_tasks__enabled=true
ai_groupmate_scheduled_tasks__min_delay_seconds=10
ai_groupmate_scheduled_tasks__max_delay_seconds=604800
ai_groupmate_scheduled_tasks__misfire_grace_time_seconds=300
ai_groupmate_scheduled_tasks__agent_history_limit=20
ai_groupmate_scheduled_tasks__record_text_history=true
ai_groupmate_scheduled_tasks__default_private=false
```

说明：

- `min_delay_seconds`：最短延迟，默认 10 秒。
- `max_delay_seconds`：最长延迟，默认 7 天。
- `misfire_grace_time_seconds`：bot 错过执行时间后的宽限秒数，默认 300。
- `agent_history_limit`：定时 agent 任务到点后读取最近多少条聊天记录，默认 20。
- `record_text_history`：固定文本定时发送成功后是否写入 `ChatHistory`。
- `default_private`：当宿主自定义工具上下文没有提供私聊标记时，是否按私聊发送。群聊 bot 保持默认 `false` 即可。

## 使用方式

用户可以自然表达：

- 10 分钟后提醒我喝水
- 两小时后发一句“该开会了”
- 今晚 21:00 查一下天气，提醒我要不要带伞

Agent 会按任务类型选择：

- 固定内容提醒、转告、发送文本：`schedule_message`
- 到点后需要查最新信息、搜索、挑表情包、重新判断上下文：`schedule_agent_task`

`schedule_agent_task` 会在执行时读取当前会话最近聊天记录，并调用主插件的 `create_chat_graph` 重新跑一轮 Agent。它和上游实现一样，没有原始消息事件，所以到点执行时不能使用消息 reaction。

## 注意事项

- APScheduler 默认内存 job store 下，bot 重启后未执行的任务不会保留。
- `run_at` 使用 bot 本地时间，格式为 `YYYY-MM-DD HH:MM` 或 `YYYY-MM-DD HH:MM:SS`。
- 工具创建成功只代表任务已登记，不代表已经执行。
