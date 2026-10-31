"""保留旧二元组接口，同时把本次发送的提交证据传回队列。"""


class SendOutcome(tuple):
    def __new__(cls, ok, message, diagnostic=None):
        value = super().__new__(cls, (bool(ok), str(message)))
        value.diagnostic = dict(diagnostic or {})
        return value


def not_submitted(message):
    return SendOutcome(False, message, {'submission_state': 'not_submitted', 'send_key': False})


class SendFailure(RuntimeError):
    def __init__(self, message, diagnostic=None):
        super().__init__(message)
        self.diagnostic = dict(diagnostic or {})


def failure_state(exc, *, sending=False, chunks_sent=0):
    """只有本次明确尚未提交才标 failed；旧后端缺少证据时仍保守待核实。"""
    diagnostic = getattr(exc, 'diagnostic', {})
    if chunks_sent or diagnostic.get('partial_content'):
        return 'uncertain'
    if not sending:
        return 'failed'
    if (not chunks_sent and not diagnostic.get('partial_content') and
            diagnostic.get('submission_state') == 'not_submitted' and
            diagnostic.get('send_key') is not True):
        return 'failed'
    return 'uncertain'
