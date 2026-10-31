"""技能包：纯数据、权限声明、send_message 永不授予、停用不入上下文。"""
import tempfile
import unittest
from pathlib import Path

from src.agent_skills import SkillError, SkillStore, match, validate

GOOD = {"format": "whale-agent-skill", "version": 1, "id": "tone_help", "name": "语气调整",
        "triggers": ["语气", "说话风格"], "permissions": ["read_memory"],
        "instructions": "先问清楚要活泼还是克制"}


class ValidateTests(unittest.TestCase):
    def test_valid_skill_normalised(self):
        skill = validate(GOOD)
        self.assertEqual(skill["id"], "tone_help")
        self.assertEqual(skill["granted"], ["read_memory"])

    def test_wrong_format_rejected(self):
        with self.assertRaises(SkillError):
            validate({**GOOD, "format": "other"})

    def test_bad_id_rejected(self):
        with self.assertRaises(SkillError):
            validate({**GOOD, "id": "Bad Id"})

    def test_unknown_permission_rejected(self):
        with self.assertRaises(SkillError):
            validate({**GOOD, "permissions": ["root_shell"]})

    def test_send_message_declared_but_not_granted(self):
        skill = validate({**GOOD, "permissions": ["send_message", "read_memory"]})
        self.assertEqual(skill["granted"], ["read_memory"])
        self.assertEqual(skill["denied"], ["send_message"])

    def test_empty_triggers_rejected(self):
        with self.assertRaises(SkillError):
            validate({**GOOD, "triggers": []})


class MatchTests(unittest.TestCase):
    def test_disabled_skill_is_skipped(self):
        skills = [validate(GOOD), validate({**GOOD, "id": "off_skill", "enabled": False})]
        matched = match(skills, "帮我调一下语气")
        self.assertEqual([s["id"] for s in matched], ["tone_help"])

    def test_trigger_matching(self):
        self.assertEqual(match([validate(GOOD)], "今天天气不错"), [])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SkillStore(Path(self.tmp.name) / 'skills.json')

    def tearDown(self):
        self.tmp.cleanup()

    def test_preview_does_not_persist(self):
        self.store.preview(GOOD)
        self.assertEqual(self.store.skills(), [])

    def test_import_and_toggle_and_delete(self):
        self.store.import_skill(GOOD)
        self.assertEqual(len(self.store.skills()), 1)
        self.assertFalse(self.store.toggle("tone_help")["enabled"])
        self.assertTrue(self.store.delete("tone_help"))
        self.assertFalse(self.store.delete("tone_help"))

    def test_duplicate_requires_overwrite(self):
        self.store.import_skill(GOOD)
        with self.assertRaises(SkillError):
            self.store.import_skill(GOOD)
        skill = self.store.import_skill({**GOOD, "name": "语气调整2"}, overwrite=True)
        self.assertEqual(skill["name"], "语气调整2")


if __name__ == '__main__':
    unittest.main()
