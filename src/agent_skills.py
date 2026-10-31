"""Agent 技能包（1.3.0 P1-1）：**纯数据**，不执行任何代码。

一个技能就是一段"什么时候该怎么做"的说明：

    {"format":"whale-agent-skill","version":1,"id":"tone_help","name":"语气调整",
     "triggers":["语气","说话风格"],"permissions":["read_memory"],
     "instructions":"先问清楚要活泼还是克制"}

为什么坚持纯数据：技能包是从外部导入的，一旦允许带代码就等于让陌生人往控制台里塞插件；
这里只接受 JSON 字段，权限只做**声明**，真正能不能做仍由 `wechat_agent` 的白名单决定。
`send_message` 即使在包里声明了也**永不授予**（微信端硬约束）。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

FORMAT = "whale-agent-skill"
ALLOWED_PERMISSIONS = {"read_memory", "modify_settings", "toggle_plugins",
                       "switch_persona", "send_message", "read_history", "network", "write_files"}
DENIED_PERMISSIONS = {"send_message"}          # 声明了也不授予
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class SkillError(ValueError):
    """可直接展示给用户的错误。"""


def validate(raw) -> dict:
    """校验并归一化一个技能包；不合法就抛 SkillError（错误信息面向用户）。"""
    if not isinstance(raw, dict):
        raise SkillError("技能包必须是一个 JSON 对象")
    if raw.get("format") != FORMAT:
        raise SkillError(f'缺少 format="{FORMAT}"，这不是微信端的技能包')
    if int(raw.get("version") or 0) != 1:
        raise SkillError("只支持 version=1 的技能包")
    skill_id = str(raw.get("id") or "").strip()
    if not ID_RE.match(skill_id):
        raise SkillError("id 只能是小写字母开头、字母数字下划线，长度 2~41")
    name = str(raw.get("name") or "").strip()
    if not name or len(name) > 40:
        raise SkillError("name 必填，最多 40 字")
    triggers = raw.get("triggers") or []
    if not isinstance(triggers, list) or not 1 <= len(triggers) <= 20:
        raise SkillError("triggers 要是 1~20 条的数组")
    triggers = [str(item).strip() for item in triggers if str(item).strip()]
    if not triggers:
        raise SkillError("triggers 不能为空")
    permissions = raw.get("permissions") or []
    if not isinstance(permissions, list):
        raise SkillError("permissions 要是数组")
    unknown = [str(p) for p in permissions if str(p) not in ALLOWED_PERMISSIONS]
    if unknown:
        raise SkillError("未知权限：" + "、".join(unknown))
    granted = [str(p) for p in permissions if str(p) not in DENIED_PERMISSIONS]
    instructions = str(raw.get("instructions") or "").strip()
    if not instructions or len(instructions) > 2000:
        raise SkillError("instructions 必填，最多 2000 字")
    return {"id": skill_id, "name": name, "description": str(raw.get("description") or "")[:200],
            "triggers": triggers, "permissions": list(dict.fromkeys(str(p) for p in permissions)),
            "granted": granted, "denied": [p for p in permissions if str(p) in DENIED_PERMISSIONS],
            "instructions": instructions, "enabled": bool(raw.get("enabled", True))}


def match(skills, text: str) -> list:
    """按触发词挑技能；**停用的不参与**（计划要求：停用后不进规划上下文）。"""
    haystack = str(text or "")
    return [skill for skill in skills
            if skill.get("enabled") and any(word in haystack for word in skill.get("triggers") or [])]


class SkillStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("skills"), list):
                return data["skills"]
        except (OSError, ValueError):
            pass
        return []

    def _save(self, skills: list) -> None:
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps({"skills": skills}, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(self.path)

    def skills(self) -> list:
        return self._load()

    def get(self, skill_id: str):
        return next((skill for skill in self._load() if skill["id"] == skill_id), None)

    def preview(self, raw) -> dict:
        """导入预览：**不落盘**，只回校验结果与"哪些权限会被拒绝"。"""
        skill = validate(raw)
        return {"skill": skill, "exists": self.get(skill["id"]) is not None}

    def import_skill(self, raw, overwrite: bool = False) -> dict:
        skill = validate(raw)
        skills = self._load()
        existing = next((s for s in skills if s["id"] == skill["id"]), None)
        if existing is not None and not overwrite:
            raise SkillError(f"技能 {skill['id']} 已存在（覆盖请勾选覆盖导入）")
        skills = [s for s in skills if s["id"] != skill["id"]]
        skill["imported"] = time.time()
        skills.append(skill)
        self._save(skills[:100])
        return skill

    def toggle(self, skill_id: str, enabled: bool | None = None) -> dict:
        skills = self._load()
        for skill in skills:
            if skill["id"] != skill_id:
                continue
            skill["enabled"] = (not skill.get("enabled")) if enabled is None else bool(enabled)
            self._save(skills)
            return skill
        raise SkillError("技能不存在")

    def delete(self, skill_id: str) -> bool:
        skills = self._load()
        kept = [s for s in skills if s["id"] != skill_id]
        if len(kept) == len(skills):
            return False
        self._save(kept)
        return True
