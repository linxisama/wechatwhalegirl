"""宿主仅原位发布既有请求镜像，保留同一请求身份与入口预算。"""
from __future__ import annotations

import math
import os
import re
import time

from .ocr_slots import _write_existing
from .protocol import (MAX_DOCUMENT_BYTES, ProtocolError, _bytes, checked_path,
                       request_digest, signed, valid_epoch, validate_request, verify)


def publish_request_mirror(path, pairing, payload, *, epoch, digest, deadline):
    path = checked_path(path, pairing)
    if path != checked_path(pairing.root / 'control/request.json', pairing):
        raise ProtocolError('请求镜像仅允许既有固定槽')
    if (not valid_epoch(epoch) or not isinstance(digest, str)
            or re.fullmatch(r'[0-9a-f]{64}', digest) is None
            or type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ProtocolError('请求镜像发布身份或截止时间无效')
    envelope = signed(pairing, payload)
    request = verify(pairing, envelope)
    validate_request(request, epoch, now=time.time())
    if request_digest(request) != digest:
        raise ProtocolError('请求镜像与本次原请求摘要不符')
    raw = _bytes(envelope)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ProtocolError('请求镜像超过大小限制')
    # 验证和签名所花时间也计入入口的原预算；不能在文件发布后重新起算。
    monotonic_now, wall_now = time.monotonic(), time.time()
    remaining = min(deadline - monotonic_now, request['deadline'] - wall_now)
    if remaining <= 0:
        raise TimeoutError('原请求发布预算已耗尽')
    if os.name != 'nt':
        raise OSError('既有请求镜像发布仅支持 Windows')
    # 复用已验证的 OPEN_EXISTING + share=0 单次写入；只有写入前的
    # WinError32 句柄竞争可等待，槽丢失、写入后异常均不得重建或降级。
    _write_existing(path, raw, deadline=min(deadline, monotonic_now + min(.05, remaining)))
    return path
