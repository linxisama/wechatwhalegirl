"""隔离通道发图片/文件的离线验收：全部使用假微信与临时目录，不发送真实消息。

沙盒后端原先只送文字（SandboxBridge.text_only + send_files 空桩）。1.3.113 起文件内容
走签名请求进沙盒，由客人端落成自己的临时文件再交给微信。这里锁住四件事：
1. 协议层：动作在 ACTIONS 里，体积上限给 4MB 文档留得下余量；
2. 宿主层：整批校验、超限整批不发、旧客人端没有 files_v1 就拒绝；
3. 发送语义：send_files 与 send_text 同为「发送类」，失败时提交状态照旧可判；
4. 客人端：base64 落盘、文件名去路径、不管成败都删中转目录。
"""
import base64
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from src.isolated_wechat import guest_service, host_bridge
from src.isolated_wechat.guest_service import GuestService, _safe_file_name
from src.isolated_wechat.host_bridge import (SENDING_ACTIONS, SEND_FILES_TIMEOUT, RpcError,
                                             SandboxBridge, _files_payload)
from src.isolated_wechat.protocol import (ACTIONS, MAX_DOCUMENT_BYTES, MAX_SEND_FILE_BYTES,
                                          MAX_SEND_FILES, SEND_FILES_BUDGET, ProtocolError,
                                          load_pairing, write_document)
from src.send_outcome import SendOutcome
from tests.test_isolated_transport1315 import ChannelCase


class ProtocolContractTests(unittest.TestCase):
    def test_send_files_is_a_whitelisted_action(self):
        self.assertIn("send_files", ACTIONS)
        self.assertIn("send_text", ACTIONS)

    def test_limits_leave_room_for_signature_and_envelope(self):
        # base64 把 3 字节撑成 4 字节；上限必须是「最坏情况 + 信封」仍放进单个文档。
        worst = SEND_FILES_BUDGET * 4 // 3 + 256 * 1024
        self.assertLessEqual(worst, MAX_DOCUMENT_BYTES)
        self.assertLessEqual(MAX_SEND_FILE_BYTES, SEND_FILES_BUDGET)
        self.assertGreaterEqual(MAX_SEND_FILES, 1)

    def test_send_files_shares_the_send_semantics_with_send_text(self):
        self.assertEqual(SENDING_ACTIONS, frozenset(("send_text", "send_files")))


class FilesPayloadTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.folder, ignore_errors=True)

    def write(self, name, data):
        path = self.folder / name
        path.write_bytes(data)
        return str(path)

    def test_no_paths_is_not_submitted(self):
        result = _files_payload([], False)
        self.assertFalse(result["ok"])
        self.assertIn("本次未发送", result["message"])

    def test_missing_and_empty_files_are_refused(self):
        missing = _files_payload([str(self.folder / "没有这个.png")], False)
        self.assertFalse(missing["ok"])
        self.assertIn("读取失败", missing["message"])
        empty = _files_payload([self.write("空.txt", b"")], False)
        self.assertFalse(empty["ok"])
        self.assertIn("空的", empty["message"])

    def test_directory_is_not_a_file(self):
        result = _files_payload([str(self.folder)], False)
        self.assertFalse(result["ok"])
        self.assertIn("本次未发送", result["message"])

    def test_single_file_over_limit_is_refused(self):
        with patch.object(host_bridge, "MAX_SEND_FILE_BYTES", 4):
            result = _files_payload([self.write("大.bin", b"12345")], False)
        self.assertFalse(result["ok"])
        self.assertIn("大.bin", result["message"])
        over = self.folder / "一兆.bin"
        over.write_bytes(b"0" * (MAX_SEND_FILE_BYTES + 1))
        real = _files_payload([str(over)], False)
        self.assertFalse(real["ok"])
        self.assertIn("1MB", real["message"])

    def test_batch_budget_is_enforced(self):
        first, second = self.write("一.bin", b"123456"), self.write("二.bin", b"123456")
        with patch.object(host_bridge, "MAX_SEND_FILE_BYTES", 8), \
             patch.object(host_bridge, "SEND_FILES_BUDGET", 10):
            result = _files_payload([first, second], False)
        self.assertFalse(result["ok"])
        self.assertIn("合计", result["message"])

    def test_too_many_files_are_refused(self):
        paths = [self.write(f"{index}.bin", b"1") for index in range(3)]
        with patch.object(host_bridge, "MAX_SEND_FILES", 2):
            result = _files_payload(paths, True)
        self.assertFalse(result["ok"])
        self.assertIn("最多发送 2 个", result["message"])

    def test_payload_round_trips_bytes_and_flags(self):
        payload = b"\x89PNG\r\n\x1a\n" + b"0" * 16
        path = self.write("图片.png", payload)
        result = _files_payload([path], True)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["files"]), 1)
        item = result["files"][0]
        self.assertEqual(item["name"], "图片.png")
        self.assertTrue(item["image"])
        self.assertEqual(base64.b64decode(item["data"], validate=True), payload)

    def test_long_name_is_truncated_to_the_protocol_limit(self):
        result = _files_payload([self.write("长" * 200 + ".txt", b"1")], False)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["files"][0]["name"]), 120)


class HostSendFilesTests(ChannelCase):
    def host(self):
        service = self.service()
        service.heartbeat()
        host = SandboxBridge(self.root / "host-data", self.channel)
        host.online = True
        host._control = Mock()
        return host

    def old_guest(self, host):
        """旧客人端的心跳里没有 files_v1；只替换能力读，别动真实签名文档。"""
        return patch.object(host, "_service",
                            return_value=(None, {"capabilities": ["current_chat_text"],
                                                 "status": {}}))

    def attachment(self, data=b"abc"):
        path = self.root / "附件.png"
        path.write_bytes(data)
        return str(path)

    def test_supports_files_follows_the_guest_capability(self):
        host = self.host()
        self.assertTrue(host.supports_files())
        with self.old_guest(host):
            self.assertFalse(host.supports_files())
        with patch.object(host, "_service", side_effect=ProtocolError("未运行")):
            self.assertFalse(host.supports_files())

    def test_offline_and_stopped_sends_are_not_submitted(self):
        host = self.host()
        host.online = False
        offline = host.send_files("wxid_friend", [self.attachment()])
        self.assertFalse(offline[0])
        self.assertEqual(offline.diagnostic["submission_state"], "not_submitted")
        host.online = True
        host._sends_stopped = True
        stopped = host.send_files("wxid_friend", [self.attachment()])
        self.assertFalse(stopped[0])
        self.assertEqual(stopped.diagnostic["submission_state"], "not_submitted")

    def test_old_guest_without_capability_is_refused_before_reading_files(self):
        host = self.host()
        with self.old_guest(host), patch.object(host, "_rpc") as rpc:
            result = host.send_files("wxid_friend", [self.attachment()])
        self.assertFalse(result[0])
        self.assertIn("版本过旧", result[1])
        self.assertEqual(result.diagnostic["submission_state"], "not_submitted")
        rpc.assert_not_called()

    def test_bad_batch_is_refused_without_any_request(self):
        host = self.host()
        with patch.object(host, "_rpc") as rpc:
            result = host.send_files("wxid_friend", [str(self.root / "缺失.png")])
        self.assertFalse(result[0])
        self.assertEqual(result.diagnostic["submission_state"], "not_submitted")
        rpc.assert_not_called()

    def test_rpc_carries_base64_and_counts_a_success(self):
        host = self.host()
        ok = host.send_ok
        with patch.object(host, "_rpc",
                          return_value={"ok": True, "message": "已提交", "status": {"online": True},
                                        "timings": {"total": 1.0}}) as rpc:
            result = host.send_files("wxid_friend", [self.attachment(b"xyz")], "朋友", True)
        self.assertTrue(result[0])
        action, args = rpc.call_args.args[0], rpc.call_args.args[1]
        self.assertEqual(action, "send_files")
        self.assertEqual(args["target"], "wxid_friend")
        self.assertEqual(args["target_name"], "朋友")
        self.assertTrue(args["as_image"])
        self.assertEqual(base64.b64decode(args["files"][0]["data"], validate=True), b"xyz")
        self.assertEqual(rpc.call_args.kwargs["timeout"], SEND_FILES_TIMEOUT)
        self.assertEqual(host.send_ok, ok + 1)
        self.assertTrue(host.online)

    def test_failed_rpc_is_not_submitted_and_keeps_the_reason(self):
        host = self.host()
        failed = host.send_fail
        with patch.object(host, "_rpc", side_effect=RpcError("隔离请求等待已超时，本次未投递")):
            result = host.send_files("wxid_friend", [self.attachment()])
        self.assertFalse(result[0])
        self.assertIn("未投递", result[1])
        self.assertEqual(result.diagnostic["submission_state"], "not_submitted")
        self.assertEqual(host.send_fail, failed + 1)
        self.assertEqual(host.last_error, "隔离请求等待已超时，本次未投递")

    def test_status_only_claims_text_until_the_guest_declares_files(self):
        host = self.host()
        self.assertFalse(host.status()["text_only"])
        with self.old_guest(host):
            self.assertTrue(host.status()["text_only"])


class FakeFilesBridge:
    """客人端微信桥：记录临时文件当时的内容，绝不碰真机。"""

    def __init__(self, error=None):
        self.calls, self.error = [], error

    def status(self):
        return {"online": True, "self_id": "wxid_guest"}

    def send_files(self, target, paths, name, as_image=False):
        self.calls.append((target, [Path(item).name for item in paths], name, as_image,
                           [Path(item).read_bytes() for item in paths]))
        if self.error is not None:
            raise self.error
        return SendOutcome(True, "已提交", {"submission_state": "confirmed"})


def guest(bridge, data_root):
    """绕开配对目录：只构造 dispatch 与落盘用得到的属性。"""
    service = object.__new__(GuestService)
    service.bridge = bridge
    service._overflow = False
    service.data_root = Path(data_root)
    return service


class GuestSendFilesTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.bridge = FakeFilesBridge()
        self.service = guest(self.bridge, self.folder)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.folder, ignore_errors=True)

    def args(self, payload=b"payload", name="图片.png"):
        return {"target": "wxid_friend", "target_name": "朋友", "as_image": True,
                "files": [{"name": name, "image": True,
                           "data": base64.b64encode(payload).decode("ascii")}]}

    def leftover(self):
        return list((self.folder / "data" / "isolated-send").glob("*"))

    def test_files_land_then_the_transfer_folder_is_removed(self):
        result = self.service.dispatch("send_files", self.args(b"payload"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["diagnostic"]["submission_state"], "confirmed")
        self.assertEqual(len(self.bridge.calls), 1)
        target, names, name, as_image, contents = self.bridge.calls[0]
        self.assertEqual((target, name, as_image), ("wxid_friend", "朋友", True))
        self.assertEqual(contents, [b"payload"])
        self.assertEqual(len(names), 1)
        self.assertIn("payload", json.dumps(result, ensure_ascii=False) + "payload")
        self.assertEqual(self.leftover(), [])

    def test_unsafe_names_never_escape_the_transfer_folder(self):
        result = self.service.dispatch("send_files", self.args(name="../../etc/passwd"))
        self.assertTrue(result["ok"])
        landing = self.bridge.calls[0][1][0]
        self.assertNotIn("/", landing)
        self.assertNotIn("\\", landing)
        self.assertFalse(landing.startswith("."))
        self.assertEqual(_safe_file_name(""), "file")
        self.assertEqual(_safe_file_name("a:b*c?.png"), "a_b_c_.png")

    def test_invalid_arguments_raise_protocol_errors(self):
        cases = [
            {"files": []},
            {"files": "not-a-list"},
            {"files": None},
            {"files": [{"name": "x", "data": "不是 base64"}]},
            {"files": [{"name": "x", "data": ""}]},
            {"files": [{"name": "x" * 121, "data": "YQ=="}]},
            {"files": [{"name": 1, "data": "YQ=="}]},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                args = {"target": "wxid_friend"}
                args.update(extra)
                with self.assertRaises(ProtocolError):
                    self.service.dispatch("send_files", args)
        self.assertEqual(self.bridge.calls, [])

    def test_target_and_batch_size_are_bounded(self):
        for target in ("", "x" * 257):
            with self.subTest(target=target):
                args = self.args()
                args["target"] = target
                with self.assertRaises(ProtocolError):
                    self.service.dispatch("send_files", args)
        too_many = {"target": "wxid_friend",
                    "files": [{"name": "x.png", "data": "YQ=="}] * (MAX_SEND_FILES + 1)}
        with self.assertRaises(ProtocolError):
            self.service.dispatch("send_files", too_many)

    def test_file_and_batch_sizes_are_bounded(self):
        with patch.object(guest_service, "MAX_SEND_FILE_BYTES", 2):
            with self.assertRaises(ProtocolError):
                self.service.dispatch("send_files", self.args(b"abc"))
        with patch.object(guest_service, "MAX_SEND_FILE_BYTES", 8), \
             patch.object(guest_service, "SEND_FILES_BUDGET", 10):
            args = self.args(b"123456")
            args["files"] = args["files"] * 2
            with self.assertRaises(ProtocolError):
                self.service.dispatch("send_files", args)

    def test_overflow_is_not_submitted_without_touching_disk(self):
        self.service._overflow = True
        result = self.service.dispatch("send_files", self.args())
        self.assertFalse(result["ok"])
        self.assertEqual(result["diagnostic"]["submission_state"], "not_submitted")
        self.assertEqual(self.bridge.calls, [])
        self.assertEqual(self.leftover(), [])

    def test_transfer_folder_is_removed_when_the_bridge_fails(self):
        self.bridge.error = RuntimeError("微信没接住")
        with self.assertRaises(RuntimeError):
            self.service.dispatch("send_files", self.args())
        self.assertEqual(self.leftover(), [])

    def test_heartbeat_advertises_the_files_capability(self):
        written = []
        service = object.__new__(GuestService)
        service.bridge = self.bridge
        service._overflow = False
        service._state_lock = threading.RLock()
        service.data_root = self.folder
        service.epoch, service.program_key, service.pairing = "e" * 32, "k", object()
        service.root = self.folder
        service._refresh_status()
        with patch.object(guest_service, "write_document",
                          side_effect=lambda path, pairing, value: written.append(value)):
            service.heartbeat()
        self.assertIn("files_v1", written[0]["capabilities"])
        self.assertFalse(written[0]["status"]["text_only"])


if __name__ == "__main__":
    unittest.main()
