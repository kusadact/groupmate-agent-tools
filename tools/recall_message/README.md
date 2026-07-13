# recall_message

让 `nonebot-plugin-groupmate-agent` 在判断合理时撤回消息的外部工具。

## 功能

- 撤回 bot 自己发错、重复发送或明显不合适的消息。
- bot 有群管理权限时，可以在理由充分时撤回他人消息。
- 用户要求撤回只作为参考信号；工具会拒绝“只因为用户要求”的撤回。
- 工具不发送群聊文案，只返回内部事实结果，让 agent 按 bot 人设自然决定要不要解释。

## 安装

复制本目录到主插件的数据目录：

```text
data/nonebot_plugin_groupmate_agent/tools/recall_message
```

目录中需要包含：

```text
__init__.py
README.md
```

不需要额外 Python 依赖。

## 运行要求

- OneBot v11 适配器。
- bot 实例支持 `get_msg` 和 `delete_msg`，或支持 `call_api("get_msg")` / `call_api("delete_msg")`。
- 撤回他人消息时，bot 必须是群主或管理员。

## 合理性规则

允许的撤回类别：

- `bot_mistake`：bot 自己发错、重复发送、答非所问或内容明显不合适。
- `spam_or_ad`：垃圾刷屏、广告、推广、引流。
- `scam_or_malicious_link`：诈骗、钓鱼、盗号、恶意链接。
- `privacy_leak`：手机号、住址、身份证、银行卡等隐私泄露。
- `sexual_violent_or_illegal`：色情、血腥暴力、违法内容。
- `harassment_or_hate`：骚扰、威胁、仇恨或严重人身攻击。
- `malicious_disruption`：恶意破坏群聊秩序、轰炸、冒充、带节奏。
- `user_self_sensitive`：用户本人请求撤回自己误发的敏感信息。

默认拒绝：

- 只因为用户说“撤回”。
- 没有具体证据。
- 普通争吵、不同意见、轻微冒犯。
- bot 没有管理权限时撤回他人消息。
- 撤回管理员或群主的消息。
- 用 `bot_mistake` 撤回非 bot 消息。
- 用 `user_self_sensitive` 撤回别人发的消息，或不是用户本人提出的请求。

## 模型行为

工具 prompt 会要求 agent：

- 调用前必须自己判断合理性。
- 调用时填写 `reason_category`、`reason`、`evidence`、`requested_by_user`。
- 不要机械复读工具返回。
- 成功或拒绝后，是否回复由 bot 根据人设和场景决定。

示例自然回复可以是：

```text
这条我先撤了，别刷广告。
刚那句我撤了，不太合适。
不删，没到该撤的程度。
```

这些只是风格示例，不是固定模板。
