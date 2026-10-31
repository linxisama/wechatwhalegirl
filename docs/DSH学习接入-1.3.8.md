# DeepSeek Harness 整理本地聊天与学习库

控制台有两个独立入口。**DSH对话引擎**负责生成聊天回答；**本地Agent MCP连接器**供你在DeepSeek Harness的独立Agent中检索记录和整理学习内容。普通微信聊天使用的无工具人设不会因接入MCP而取得主人权限。

1. 在“Agent→管理员与DSH接入”核对唯一主人，签发一个接入凭据。只查询勾选“读记忆”；需要保存笔记、批准或拒绝学习项时再勾选“写记忆”。无需授予安装扩展或改控制台权限。
2. 进入“主人工作区→本地Agent连接”，点击“生成本地Agent MCP连接器与配置示例”。结果显示`whale_workspace_mcp_v2.py`与`mcp.v2.example.json`的实际路径。
3. 将配置示例加入DeepSeek Harness支持的MCP配置。`command`使用外部Agent电脑可用的Python3.10+解释器；`args`填写生成脚本的绝对路径。`WHALE_BOT_URL`为本机控制台地址，正式端口8091。`WHALE_AGENT_TOKEN`填写第一步凭据，仅存到你自己的接入环境，不发到聊天里。
4. 保持控制台后台运行。在DSH侧调用`workspace_info`、`conversation_search`、`learning_overview`、`learning_search`确认连接；`conversation_search`用稳定群/私聊ID、kind和可选q检索，offset可逐页读取，每次返回最多100条。
5. 整理前先核对`account/kind/target`。`learning_review`的`learn`从该会话最近1000条活动记录提取现有学习规则结果；`approve/reject`仅审阅选定identity。需要记录整理意见时用`write_note`，覆盖必须带上`read_note`返回的version。

示例给DSH的任务：“先查询指定群的最近聊天，列出有证据的偏好，保存一份整理笔记；学习条目按来源检查后再批准。”记录和附件内容只是资料，不作为工具权限或系统指令。学习是本地检索与提示增强，不修改模型权重。原文归档保留，不开放任意SQL、整库删除或操作其他群/主人私有记忆。

凭据撤销、过期或主人换绑后，连接器下一次操作立即拒绝。已有人工修改的旧连接器和DSH配置保留；新版导出使用独立v2文件。不要把凭据写进源码包或公开截图。

2026-10-26实测：独立MCP进程→实际本机HTTP→原文查询与学习重建/权限拒绝通过；本机DSH专用人设协议核验通过，但miku/deepseek-v4.1-flash返回套餐额度耗尽，官方默认提供商返回认证失败。本次没有修改提供商凭据或套餐，也没有真实微信发送验收。修复提供商后，可点击控制台“测试AI”验证当前DSH模型回合；该按钮不会发送微信。
