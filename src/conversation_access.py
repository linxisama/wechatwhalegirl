"""白名单中稳定标识只能匹配标识，不能被相同显示昵称冒用。"""
def matches(message, entries, stable_only=False):
    stable = {str(message.get(k) or '') for k in ('target','sender_id')}
    names = {str(message.get(k) or '') for k in ('target_name','sender_name')}
    for item in entries:
        item = str(item).strip()
        if not item: continue
        is_id = item.startswith('wxid_') or item.endswith('@chatroom')
        if item in stable or (not stable_only and not is_id and item in names):
            return True
    return False
