"""语音未转写时静默跳过：既不调模型，也不把本端能力当成聊天内容发出去。"""
import re


def _is_untranscribed_voice(message):
    if message.get('voice_text'):
        return False
    if not any(isinstance(item, dict) and item.get('type') == 'voice'
               for item in message.get('media', [])):
        return False
    text = str(message.get('text') or '')
    text = re.sub(r'\[(?:语音|voice)[^\]]*\]', '', text, flags=re.I)
    text = re.sub(r'[@＠][^\s\u2005]+', '', text).strip()
    return not text


def voice_needs_no_reply(message):
    """未转写且没有附带文字的语音：本轮不回复（有附文字仍回答文字）。"""
    return _is_untranscribed_voice(message)
