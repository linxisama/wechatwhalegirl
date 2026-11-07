"""MemberDirectory 房间数上限（1.3.97）的离线测试。

房间数以前没有上限：每进一个新群或新私聊，self._rooms 就永久多一条（每条最多 200 名成员），
长期运行内存单调增长。这里钉住新增的 LRU 淘汰行为，同时保证既有的
"每房间成员上限" 与 forget() 语义没有被动过。
"""

import unittest

from src.member_directory import MemberDirectory


def msg(target, sender_id="wxid_1", sender_name="甲", kind="group"):
    return {"kind": kind, "target": target, "sender_id": sender_id, "sender_name": sender_name}


def room(target, kind="group"):
    return {"kind": kind, "target": target}


class RoomCapTests(unittest.TestCase):
    def test_default_room_cap_is_200(self):
        # 默认值必须收紧：否则长期运行每个新群都留一条，内存只涨不降
        directory = MemberDirectory()
        for index in range(201):
            directory.observe(msg(f"g{index}", sender_id=f"u{index}"))
        self.assertEqual(len(directory._rooms), 200)

    def test_oldest_rooms_are_evicted_first(self):
        directory = MemberDirectory(max_rooms=3)
        for index in range(5):
            directory.observe(msg(f"g{index}", sender_id=f"u{index}"))
        self.assertEqual(len(directory._rooms), 3)
        # 留下的必须是最后 3 个房间：淘汰从队头（最久没观察）开始
        for index in (0, 1):
            self.assertEqual(directory.members(room(f"g{index}")), [])
        for index in (2, 3, 4):
            self.assertEqual(directory.members(room(f"g{index}")),
                             [{"userId": f"u{index}", "name": "甲"}])

    def test_reobserved_room_is_moved_to_the_back(self):
        # 纯 FIFO 会淘汰 A；LRU 必须淘汰 B —— observe 要刷新"最近"位置
        directory = MemberDirectory(max_rooms=2)
        directory.observe(msg("A", sender_id="uA"))
        directory.observe(msg("B", sender_id="uB"))
        directory.observe(msg("A", sender_id="uA2"))
        directory.observe(msg("C", sender_id="uC"))
        self.assertEqual(len(directory._rooms), 2)
        self.assertEqual([m["userId"] for m in directory.members(room("A"))], ["uA", "uA2"])
        self.assertEqual(directory.members(room("B")), [])
        self.assertEqual([m["userId"] for m in directory.members(room("C"))], ["uC"])

    def test_room_being_observed_survives_eviction(self):
        # 正在观察的房间被自己挤掉，等于这条消息白记了
        directory = MemberDirectory(max_rooms=1)
        directory.observe(msg("A", sender_id="uA"))
        directory.observe(msg("B", sender_id="uB"))
        self.assertEqual(len(directory._rooms), 1)
        self.assertEqual([m["userId"] for m in directory.members(room("B"))], ["uB"])
        self.assertEqual(directory.members(room("A")), [])

    def test_non_positive_room_cap_is_clamped_to_one(self):
        # 配置里填 0 或负数不能让名录彻底不记东西，也不能崩
        for raw in (0, -1, -999):
            with self.subTest(max_rooms=raw):
                directory = MemberDirectory(max_rooms=raw)
                self.assertEqual(directory._max_rooms, 1)
                directory.observe(msg("A", sender_id="uA"))
                directory.observe(msg("B", sender_id="uB"))
                self.assertEqual(len(directory._rooms), 1)
                self.assertEqual([m["userId"] for m in directory.members(room("B"))], ["uB"])

    def test_forget_still_clears_room_and_frees_a_slot(self):
        directory = MemberDirectory(max_rooms=2)
        directory.observe(msg("A", sender_id="uA"))
        directory.observe(msg("B", sender_id="uB"))
        directory.forget(room("A"))
        self.assertEqual(directory.members(room("A")), [])
        self.assertEqual(len(directory._rooms), 1)
        # 腾出的位置能重新装一个新房间，淘汰逻辑不因为 forget 而错乱
        directory.observe(msg("C", sender_id="uC"))
        self.assertEqual(len(directory._rooms), 2)
        self.assertEqual([m["userId"] for m in directory.members(room("B"))], ["uB"])
        self.assertEqual([m["userId"] for m in directory.members(room("C"))], ["uC"])

    def test_per_room_member_cap_still_applies(self):
        directory = MemberDirectory(max_per_room=3, max_rooms=2)
        for index in range(5):
            directory.observe(msg("g", sender_id=f"u{index}", sender_name=f"人{index}"))
        self.assertEqual(len(directory.members(room("g"))), 3)
        # 丢的是最早记下的那两个
        self.assertEqual([m["userId"] for m in directory.members(room("g"))], ["u2", "u3", "u4"])

    def test_members_does_not_refresh_lru_order(self):
        # 读路径不改变淘汰顺序：查一次名录不该把某个房间"续命"
        directory = MemberDirectory(max_rooms=2)
        directory.observe(msg("A", sender_id="uA"))
        directory.observe(msg("B", sender_id="uB"))
        self.assertEqual(len(directory.members(room("A"))), 1)
        directory.observe(msg("C", sender_id="uC"))
        self.assertEqual(directory.members(room("A")), [])
        self.assertEqual([m["userId"] for m in directory.members(room("B"))], ["uB"])
        self.assertEqual([m["userId"] for m in directory.members(room("C"))], ["uC"])


if __name__ == "__main__":
    unittest.main()
