# AgentSkill 迁移说明

本次迁移适配 `nonebot-plugin-groupmate-agent` 的按需 `AgentSkill`（至少需要主插件 `3faf9e8` 或后续版本）。动态工具目录加载仍由主插件负责；本仓库只在每个 `OptionalToolBundle` 中声明技能和延迟开放的工具名。

## 迁移范围

当前工作区的全部 10 个工具都将长规则从 Bundle 常驻 `prompt` 移到一个按需 Skill：

| 工具 | Skill | 延迟开放的工具 | 常驻 prompt |
| --- | --- | --- | --- |
| `annual_report` | `annual_report` | `generate_and_send_annual_report` | 无 |
| `chat_stats_sql` | `chat_statistics` | `query_chat_stats_sql` | 无 |
| `danbooru_setu` | `danbooru_setu` | `send_danbooru_setu` | 无 |
| `find_femboy` | `find_femboy` | `find_femboy_in_recent_chat` | 无 |
| `gpt_image_agent` | `image_generation` | `generate_and_send_image` | 无 |
| `read_forward_message` | `read_forward_message` | `read_forward_message` | 无 |
| `recall_message` | `recall_message` | `recall_message` | 无 |
| `scheduled_tasks` | `scheduled_tasks` | `schedule_message`, `schedule_agent_task` | 无 |
| `voice` | `voice_synthesis` | `send_voice` | 无 |
| `poke` | `poke` | `poke_user` | 无 |

每个 `AgentSkill.description` 只保留首轮路由所需的一句短描述，原有的触发条件、参数规则、发送/后台任务行为、失败处理和上下文限制都保留在 `AgentSkill.prompt` 中。健康检查、`build(ctx)` 的上下文判断、`ToolLimitSpec`、请求过期保护、权限检查和 detached 任务逻辑没有改变。

## 可见性行为

- 健康检查成功后，Bundle 和 Skill 索引会被动态加载。
- 首轮只出现 `load_agent_skill` 和短的 Skill 索引；上述业务工具 schema 不会常驻。
- 模型调用正确的 `load_agent_skill` 后，完整规则返回为工具结果，并在下一轮开放该 Skill 的 `tool_names`。
- 直接伪造未开放工具调用会被主插件拒绝，不会执行工具函数。
- Skill Prompt 加载失败时，主插件不会把该 Skill 标记为已激活，关联工具继续保持不可见。
- 未声明 Skill 的旧 Bundle 仍按原逻辑保持基础工具可见；本仓库没有改动态 loader，也没有在工具仓库实现 `load_agent_skill`。

## 验证

`tests/test_agent_skill_migration.py` 检查所有工具的 Skill 声明、工具名映射、常驻 prompt 移除，以及主插件 Skill gate 的加载前、加载后和失败路径。建议在主插件测试容器中运行：

```bash
docker cp tests/. aibot-test-nonebot:/tmp/groupmate-agent-tools-tests/
docker exec aibot-test-nonebot sh -lc \
  'cd /workspace && GROUPMATE_AGENT_SRC=/workspace/src/nonebot_plugin_groupmate_agent \
   uv run python -m pytest /tmp/groupmate-agent-tools-tests/test_agent_skill_migration.py'
```

另外，10 个工具入口均通过 Python 3.12 `py_compile` 检查，动态目录导入仍使用主插件现有 loader。

## 首轮 token 对比

迁移前，10 份完整规则的静态文本约 4,393 字符；迁移后，Bundle 常驻规则为 0 字符，技能索引（含主插件固定索引说明）约 498 字符。按中文约 2.8 字符/token 的粗略估算，首轮规则部分约从 1,569 token 降到 178 token，静态文本减少约 88.7%；这还没有把 10 个业务工具 schema 从首轮解绑带来的节省计算进去。旧值不包含运行时 f-string 展开的动态字段，新值不包含主 Agent 其他系统提示；实际 token 数会随模型 tokenizer 和主插件配置变化。完整规则和 schema 只在对应任务加载 Skill 后出现。
