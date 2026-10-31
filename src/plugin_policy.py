"""微信产品的插件边界；旧配置可保留，已退役能力不能重新加载。"""
RETIRED_PLUGINS=frozenset({'context_chime','repeat_echo','question_relay','at_mention',
    'scheduled_announcer','proactive_chat','silence_ping','icebreaker','affinity_proactive',
    'forward_summary','poke_counter','probability_reply','share_card','pixiv_illust',
    'send_pacing','typo_sim','emotion_state','mood_guidance','computer_helper',
    'turn_complete','quote_reply','mention_nudge_guard','chatter_guard'})
CORE_PLUGINS=frozenset({'reply_preferences','robot_admin','private_replies','keyword_trigger',
    'group_monitor','conversation_memory','chat_controls'})
