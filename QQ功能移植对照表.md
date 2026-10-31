# QQ 版 60 个插件 → 微信版可移植性对照表

> 审计时间 2026-10-15｜QQ 版 `D:\聊天机器人\qq机器人` **1.6.2**（60 个插件）｜微信版 `D:\聊天机器人\微信机器人` **1.2.20**（34 个插件）
> 方法：逐个读 QQ 源码 + 在微信侧核对接口/消息类型/第三方依赖，结论都带 `文件:行号` 证据。
> 裁决含义：`已经具备`＝微信已有等价能力｜`可直接搬`｜`要改才能搬`｜`平台不支持`（微信没有那个 API/消息类型）｜`红线不可搬`（封号风险或主动外发）｜`不值得搬`。

## 一、总览

| 裁决 | 数量 | 说明 |
| --- | --- | --- |
| 已经具备 | 29 | 同源移植或微信实现更完整，**不用再动** |
| 要改才能搬 | 9 | 微信侧接口/口径不同，改动量 S~L |
| 平台不支持 | 8 | 微信没有 QQ 的对应事件或接口（详见条目） |
| 红线不可搬 | 11 | 主动外发、戳一戳、点表情面板等，`AGENTS.md` 明令不得引入 |
| 不值得搬 | 3 | 能搬但价值≈0 或与现有能力重复 |

**结论一句话**：真正值得动手的只有「要改才能搬」那一档里的少数几个（见第三节优先级），其余要么已有、要么平台不给、要么踩红线。

## 二、对照表

| # | QQ 插件 | 名称 | 做什么 | 裁决 | 微信现状 | 工作量 | 价值 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | addressee_guard | 点名判定 | 有人叫名字但没 @ 时提示模型可以回应 | 已经具备 | `src/plugins/addressee_guard.py`，实现比 QQ 更完整（走 `runtime.aliases()`） | S | 低 |
| 2 | affinity | 群友好感度 | 按发言/@/接话累计好感度与等级，可查排行 | 要改才能搬 | 只有 `chat_state.people` 发言计数与 `emotion_state` 心情 | M | 中 |
| 3 | affinity_proactive | 好感度主动开话题 | 按等级定时掷骰，没人说话时主动开话题 | 红线不可搬 | 已退役，加载期即拦 | S | 低 |
| 4 | anti_repeat | 打断复读 | 同内容反复触发时拦掉 | 已经具备 | `src/plugins/anti_repeat.py`，多了图片 sha256 区分 | S | 中 |
| 5 | archive_isolation | 人设存档隔离 | 切人设后历史与记忆分开存档 | 已经具备 | 由 `conversation_memory.personaIsolation` + `_persona_key` 实现 | S | 低 |
| 6 | at_mention | @群友（真 @） | 正文 `@名字` 变成对方能收到提醒的真 @ | 红线不可搬 | 1.2.17 退役；真 @ 只能走被禁的面板点击 | S | 低 |
| 7 | burst_merge | 连发合并 | 同一人连发时等他说完再合并回复 | 已经具备 | `src/plugins/burst_merge.py` + 核心 `_coalesce_pending` | S | 中 |
| 8 | cache_stability | 缓存稳定性 | 把每轮都变的说明排到提示词末尾，提升前缀缓存命中 | 要改才能搬 | 无此模块；system prompt 已是"人设在前、插件在后" | M | 低 |
| 9 | chat_controls | 群聊上下文与回复控制 | 触发回复、附近期背景、限流、备用模型 | 已经具备 | 同名插件已默认启用 | S | 高 |
| 10 | chatter_guard | 水聊免回 | 连续水话/纯表情不接话（0 token） | 已经具备 | `src/plugins/chatter_guard.py`（默认关） | S | 中 |
| 11 | context_chime | 上下文插话判定 | 花一次小模型调用判断值不值得主动接话 | 红线不可搬 | 已退役且未点名消息根本走不到钩子 | S | 低 |
| 12 | conversation_memory | 长期记忆与群友档案 | 记事实与偏好，`/记住 /记忆 /忘记 /群友` | 已经具备 | `src/plugins/conversation_memory.py`，是 QQ 的超集 | S | 高 |
| 13 | cross_chat_link | 跨会话认人 | 同一人的已确认偏好用于其他会话 | 已经具备 | 并入 `conversation_memory`（`crossChatPreferences`） | S | 中 |
| 14 | early_finish | 提前收尾（省 token） | 已交付完的一轮不再多花一次调用 | 不值得搬 | 它省的两处调用微信都不存在了 | S | 低 |
| 15 | emoji_echo | 跟着贴表情 | 群友贴表情时跟贴同一条消息 | 平台不支持 | 微信无表情回应事件，也无贴表情接口 | S | 低 |
| 16 | emotion_state | 情绪与精力 | 心情/精力/压力随时间恢复并注入语气 | 已经具备 | `src/plugins/emotion_state.py` | S | 中 |
| 17 | face_decode | QQ 表情转译 | `[表情14]` 翻成 `[表情:微笑]` | 平台不支持 | 微信表情内容加密，拿不到表情 id | S | 低 |
| 18 | face_names | 表情名称表 | QQ 表情 id→官方名的静态表（非插件） | 平台不支持 | 随 `face_decode` 一起退役；无调用点 | S | 低 |
| 19 | forward_summary | 合并转发阅读 | 展开合并转发的节点文字与图片 | 平台不支持 | 微信无"聊天记录/合并转发"消息类型 | L | 低 |
| 20 | group_cost_report | 群友花费统计 | @ 一下按区间列各人 token 用量 | 要改才能搬 | 只有全局累计用量，**没有按消息落库** | M | 中 |
| 21 | hot_trend_tracker | 热梗与热点收集 | 采集 B站/贴吧/百度热点，`/热点` 可查 | 要改才能搬 | 缺 `public_web` 模块，其余接口都齐 | S | 中 |
| 22 | icebreaker | 定时暖场 | 每隔几小时自动往指定会话发暖场消息 | 红线不可搬 | 纯定时主动外发，已退役 | S | 低 |
| 23 | image_library | 表情包与自定义图库 | 登记图片后用 `/表情 标签` 发；模型可按语境选图 | 已经具备 | 同名插件 + 已合并自动收集库 | S | 高 |
| 24 | image_understanding | 图片与表情包识别 | 图片送视觉模型识别 | 已经具备 | 同名插件，取图方式已按微信重写 | S | 高 |
| 25 | keyword_reply | 关键词自动回复 | 命中关键词直接回预设内容 | 已经具备 | 同名插件，多了 scope 与匹配方式 | S | 中 |
| 26 | long_image_reader | 长图分段阅读 | 长截图重叠切片给视觉模型，标注未读 | 已经具备 | 已作为 `image_understanding.longImage` 落地（`src/long_image.py`） | S | 中 |
| 27 | market_probe | 市场探针 | 抓插件市场目录筛条目给管理员看 | 不值得搬 | 微信没有该市场接口，只有手动装插件 | M | 低 |
| 28 | member_aliases | 群友外号对照 | 命中外号才加"称呼对照"几行帮模型认人 | 要改才能搬 | 有 `member_directory`，但没有外号表 | M | 中 |
| 29 | memes | 表情包制作 | 按模板在本机合成表情图再发出 | 要改才能搬 | 有发图通道，但没有模板引擎与素材 | L | 中 |
| 30 | mention_gate | 点名闸门 | 名单群只在被点名时开口 | 已经具备 | 同名插件，语义对齐 | S | 中 |
| 31 | mention_nudge_guard | 重复艾特忽略 | 同一人短时间多次 @ 时给模型冷静提示 | 已经具备 | 同名插件，逐行等价 | S | 低 |
| 32 | minimal_mode | 极简模式（省 token） | 按档位裁历史、封顶输出、追加说短话纪律 | 要改才能搬 | 无对应插件；`max_tokens` 目前取人设值 | M | 中 |
| 33 | msg_reaction | 消息表情回应 | 给消息气泡贴表情（可让 AI 选） | 平台不支持 | 微信没有消息级 reaction API | S | 低 |
| 34 | pc_control | 机器人控制台管理 | 管理员私聊查状态、进管理模式做变更 | 已经具备 | `robot_admin` + `admin_sessions` 覆盖 | S | 中 |
| 35 | pixiv_illust | Pixiv 插图 | `/插图` 从第三方 API 搜图并发送 | 红线不可搬 | 不可控外部图片外发，已删 | M | 低 |
| 36 | poke_counter | 戳一戳统计 | 统计被戳次数并按概率回话 | 平台不支持 | 微信没有"拍一拍"通知事件 | S | 低 |
| 37 | proactive_chat | 指定群主动开话题 | 群久无人说话时定时主动开口 | 红线不可搬 | 定时主动外发，已退役 | M | 低 |
| 38 | probability_reply | 戳一戳概率回应 | 被戳按概率回应/反戳 | 红线不可搬 | 反戳要调 `Poke`，属永不调用清单 | S | 低 |
| 39 | question_relay | 问号接龙 | 群里连问号时跟着接一个 | 红线不可搬 | 未点名插话，1.2.17 删除 | S | 低 |
| 40 | quote_reply | 引用规范 | 提示用「」而不是 > 引用 | 已经具备 | 同名插件，逐行相同 | S | 低 |
| 41 | repeat_echo | 群聊复读机 | 多人同句时跟着复读 | 红线不可搬 | 1.2.17 删除 | S | 低 |
| 42 | reply_freshness | 回复时机与引用 | 算清现在几点、要回的是不是最新、是否冷场 | 要改才能搬 | 无等价能力（`chatter_guard`/`turn_complete` 都不做时序） | S | 中 |
| 43 | reply_guards | 复读接梗 | 群里跟风短句时提示模型可以接梗 | 已经具备 | `src/plugins/reply_guards.py` 逐字节相同 | S | 低 |
| 44 | scheduled_announcer | 定时消息推送 | 按星期时刻向指定群定时发消息 | 红线不可搬 | 典型定时群发，已退役 | M | 低 |
| 45 | search_query_hygiene | 搜索词清理与联网查找 | `/搜索` 清洗关键词、抓必应摘要带链接回答 | 要改才能搬 | 无联网检索插件，缺 `public_web` | M | 中 |
| 46 | send_pacing | 打字速度 | 回复分句并插延迟标记，模拟打字节奏 | 已经具备 | `src/plugins/send_pacing.py` + `_send_with_pacing` | S | 中 |
| 47 | share_card | 分享卡片解读 | 解析转发卡片/链接元数据进上下文 | 平台不支持 | 微信拿不到卡片消息段 | M | 低 |
| 48 | short_followup | 短期跟聊 | 回复后开跟聊窗口，同一人再说无需 @ | 已经具备 | 同名插件，比 QQ 多了严格截句与人设隔离 | S | 中 |
| 49 | silence_ping | 久未说话私信关心 | 指定用户长时间不说话就主动发私信 | 红线不可搬 | 主动外发，已在 `src/plugin_policy.py` 的 `RETIRED_PLUGINS` 里 | S | 低 |
| 50 | sleep_mode | 睡眠时段免回 | 指定时间段内不回复消息 | 已经具备 | 微信版同名插件 `src/plugins/sleep_mode.py`（默认关） | S | 中 |
| 51 | speech_to_text_qq | 语音转文字（QQ 原生） | 用 QQ 自带能力把语音转成文字 | 已经具备 | 微信版用本地 ASR 实现：`src/plugins/speech_to_text_wechat.py`（默认开，语音不出本机） | S | 中 |
| 52 | sticker_admin | 表情包管理 | 本地图库窗口的开关与动图抽帧数 | 已经具备 | 1.2.20 已移植：`src/plugins/sticker_admin.py` + `src/sticker_api.py` + 界面窗口 | S | 高 |
| 53 | sticker_cadence | 每三次表情 | 每 N 次成功回复附一张图库图片 | 已经具备 | 1.2.20 重写为**真的附图**（原来只提示 emoji）：`src/plugins/sticker_cadence.py` | S | 中 |
| 54 | sticker_cap | 表情库容量清理 | 按数量/容量清理图库，默认只统计不删 | 已经具备 | 淘汰已下沉到存储层：`src/sticker_library.py:295-307`（`put` 入库即按 `last_used/created` LRU，数量+容量双限），配额由 `src/plugins/sticker_steal.py:44-45,124-125` 传入 | S | 中 |
| 55 | sticker_memory | 表情含义缓存 | 同一张表情识别一次就记住含义 | 已经具备 | 微信版同名插件（`src/plugins/sticker_memory.py`），按内容 sha256 缓存 | S | 中 |
| 56 | sticker_steal | 自动收集表情 | 把别人发来的图片/表情收进本地图库 | 已经具备 | 1.2.20 已移植：`src/plugins/sticker_steal.py`（同步登记 + 异步分批下载，默认开、默认不收私聊） | M | 高 |
| 57 | token_saver | Token 节流 | 早期历史压成一行摘要、截断过长单条 | 不值得搬 | 微信历史已被轮数+字符预算卡死，阈值永不触发 | S | 低 |
| 58 | turn_complete | 对话完成判定 | 判断对方说完没有，没说完就延后 | 已经具备 | 同名插件（默认关），已按 wxid 本地化 | S | 中 |
| 59 | typing_indicator | 正在输入提示 | 回复前发"正在输入"状态 | 平台不支持 | 微信无输入状态 API；硬做要碰键鼠红线 | S | 低 |
| 60 | typo_sim | 打字错误模拟 | 按概率注入一个错字并紧跟更正 | 已经具备 | 与 QQ 逐字节相同（SHA256 一致） | S | 低 |

## 三、真值得搬的清单（按建议优先级）

### 1. `reply_freshness` 回复时机与引用 —— 建议最先做（S，价值中）
给模型补上它现在**完全看不到的时间信息**：现在几点、要回的那条是不是最新一条、群里是不是已经冷场。
- 数据层不用改：QQ 查的 `inbox(mode,account,room,payload,state,created)` 与微信同构（`src/wechat_app.py:258-260`），`room` 都是 `kind:target`。
- 要改两处口径：把说话人标识从 `QQ:<uid>` 换成 wxid/微信名（`qq机器人/src/plugins/reply_freshness.py:52-53`）；别名改读 `runtime.aliases()`（`qq机器人/src/plugins/reply_freshness.py:191-193` → 微信 `src/wechat_app.py:455`）。
- 钩子齐全：`on_ingest` 早于 `on_block`（`src/wechat_app.py:672`、`:682`），`on_pre_ai` 也能往 conversation 末条追加。
- 顺带对用户刚报的"复读"有帮助：模型知道"这条已经隔了两小时"，就不会顺着上一句改写。

### 2. `member_aliases` 群友外号对照 —— S/M，价值中
群里认人明显变准（谁说话就告诉模型这人还叫什么）。
- 注意群聊可能拿不到稳定 wxid：契约要求**缺失时留空、不得回退昵称**（`instructions/WECHAT-CONTRACT.md:101`），而 `src/member_directory.py:224` 恰好把昵称当兜底，按"非空"判定会静默失效，需要显式区分。
- 注入方式从 QQ 的 `volatile_hint` 改成 `extra_system`（微信 `PluginBase` 没有那个 helper）。
- 口令回复走被动回复，不许自己外发；管理员判定用 `robot_admin.is_admin`。

### 3. `group_cost_report` 群友花费统计 —— M，价值中
用户能问"这个群花了多少钱"。
- 前置改动：微信**从未把每轮用量落库**（QQ 由 `qq_app.py:1105-1122` 写 `message['_usage']`），要先在生成回复处补记 per-message 用量，否则报表没数据。
- 状态过滤去掉 `'partial'`（微信只写 `'sent'`）。
- 别照抄 `message['_robot_admin']=True`：微信把它当"管理员私聊指令"，群里设了反而发不出去。

### 4. `minimal_mode` 极简模式 —— M，价值中
按档位裁历史 + 输出封顶 + "说短话"纪律，实打实省 token。
- 要动契约才能保住"输出封顶"：微信 `run_on_pre_ai` 只收 `delay/block/extra_system/on_ready`，`max_tokens` 固定取人设值（`src/wechat_app.py:1048-1052`）。
- 历史口径不同：微信由 `chat_controls.historyTurns` + `contextChars` 决定，可在 `conversation` 列表上就地裁。

### 5. `hot_trend_tracker` / `search_query_hygiene` —— S/M，价值中
一个"今天有什么热点"、一个"帮我搜一下"。都**只读不主动发**。
- 都必须自建 `public_web`（微信没有这个模块），可复用贴纸库那套受限读取：DNS 公网校验 + 跳转逐跳 + 大小上限（`src/sticker_library.py:106`、`:129`）。
- 微信群里未点名的消息在 `src/wechat_app.py:688-692` 就被丢弃，所以 `/热点`、`/搜索` **必须 @ 机器人或走私聊**（比 QQ 严，符合微信默认规则）。
- 要如实告知：这两个会真的**联网**。

### 6. `affinity` 群友好感度 / `memes` 表情包制作 / `cache_stability` —— 价值偏低或成本偏高
- `affinity`：纯本地可搬，但真正的难点是"没点名的群发言也要计分"，得挂到新加的 `on_observed` 钩子上，且落盘要挪到 `tick`（钩子内不许 IO）。
- `memes`：300 套模板 + 素材要联网下载，微信打包里没有；插件安装器还禁止 `subprocess`，内置化要另加冻结子进程入口。**成本 L，建议缓做**。
- `cache_stability`：微信的稳定前缀天然在前，省钱效果已经成立，搬过来主要是能在状态里看到缓存命中率（价值低）。

## 四、明确不能搬的两类（不要反复评估）

**平台不支持（微信没有这个能力，不是权限问题）**
`emoji_echo`、`msg_reaction`（没有消息级表情回应 API）、`face_decode`、`face_names`（表情内容加密、没有表情 id）、`poke_counter`、`probability_reply`（没有"拍一拍"事件）、`forward_summary`（没有合并转发消息类型）、`share_card`（拿不到卡片段）、`typing_indicator`（没有输入状态 API，硬做要碰键鼠红线）。

**红线不可搬（`AGENTS.md` 第二节 / `封号风险与功能取舍.md`）**
`proactive_chat`、`scheduled_announcer`、`icebreaker`、`silence_ping`、`affinity_proactive`（定时/空闲主动外发）；`context_chime`、`repeat_echo`、`question_relay`、`at_mention`（未点名插话或依赖被禁的面板点击）；`pixiv_illust`（不可控外部图片外发）；`probability_reply`、`poke_counter`（戳一戳）；`share_card` 等 QQ 专属通道。
> 微信侧这些 ID 已被 `src/plugin_policy.py` 的 `RETIRED_PLUGINS` 锁死：加载期直接 `return None`，重装/旧自定义清单也装不回来（`tests/test_wechat_safety.py:149-152` 钉住）。`speech_to_text_qq` 是**平台差异**而非红线：微信版用本地 ASR 实现了同样的能力（`src/plugins/speech_to_text_wechat.py`）。

## 五、审计中顺带发现的两个小缺口（要不要一起修，你定）

1. **`sticker_memory` 的 `stickerOnly` 开关在真正的缓存路径上不生效**
   该开关只在 `remember()` 里判断（`src/plugins/sticker_memory.py:265-281`），但生产链路走的是
   `record_description()`（同文件 `:333-337`），**不经过这个过滤** —— 也就是说用户把这个开关拨到哪边都一样。
   （QQ 版同样有此问题，属移植时一并带过来的。）修法：把判断挪进 `record_description()`，或让两条入口共用同一个校验。

2. **本地图库没有"用了多少空间"的只读查询**
   QQ 版有 `/表情容量`（`qq机器人/src/plugins/sticker_cap.py:208-212`）。微信版淘汰虽然已经做进存储层，
   但只剩 `/表情库` 报张数（`src/plugins/sticker_steal.py:142-151`），**看不到 MB 用量与配额**。
   修法：把 `/表情库` 的回复扩成"张数 + 已用 MB / 上限"，数据现成（`StickerLibrary.total_bytes()`）。
