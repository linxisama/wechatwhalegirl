"""配置管理模块：YAML 读写 + Fernet 加密敏感字段（微信版）

与 QQ 版的区别：只保留微信端需要的配置段（wechat / wechat_only / ai / style /
asr / image_reader），去掉 QQ 官方机器人、企业微信、点歌与语音合成相关的段落。
"""

import base64
from pathlib import Path
from typing import Any

import yaml
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from loguru import logger

LOCKED_STYLE_NAME = "tsundere_whale_girl"

DEFAULT_CONFIG = {
    "agent": {"context": {"history_turns":10,"max_chars":8000,"recall_limit":3},"diary":{"enabled":True}},
    "dsh": {"enabled": False, "base_url": "http://127.0.0.1:3080", "model": "deepseek-flash",
            "provider": "deepseek-official", "reasoning_effort": "", "timeout": 30, "profile": "desktop"},
    "wechat": {
        "environment": "local",
        "sandbox_channel": "",
        "sandbox_channel_id": "",
        "sandbox_id": "",
        "hook": {"enabled": False, "api_url": "http://127.0.0.1:30001",
                 "account": "", "account_path": "", "target": ""},
        # auto = 自动挑选可用后端；也可固定为 wxauto4 / wechatauto / wxauto / mock
        "backend": "auto",
        "background_submit": "invoke",  # 窗口回车投递是显式选择的实验，升级不得自动开启
        "auto_start": False,
        # 运行账号昵称（由后端探测后回填，仅供参考）
        "account": "",
        # 监控群白名单：与微信里显示的群名完全一致；为空表示不限制群
        "target_groups": [],
        "listen_private": True,
        "auto_reply": True,
        # low / normal / high：群里没被点名时的响应倾向（0 = 一律需要点名/@）
        "sensitivity": "normal",
        "reply_delay": 0.0,
        # 两条消息之间的最小间隔（秒），用于降低风控风险
        "send_interval": 1.5,
        # 超过这个秒数的消息不再回复，避免"迟到回复"
        "max_message_age": 25,
        "dedupe_window": 21600,
        # 连环消息合并窗口（秒），0 = 关闭，逐条回复
        "burst_settle_seconds": 3.0,
        "group_reply_limit": 6,
        "group_reply_window": 60,
        "context_file": "./data/wechat_contexts.json",
        "answered_cache_file": "./data/wechat_answered.json",
        "trigger_keywords": [
            "大肥鲸", "deepseek", "鲸鱼娘", "小深", "蓝鲸酱", "小鲸鱼", "出来", "在不在",
            "啥子", "做啥子", "在干嘛", "说啥呢", "咋样了", "冒个泡",
            "吃大米", "开饭了", "大米饭", "干饭了", "煲仔饭", "投喂",
            "token不够用了", "帮帮我", "救一下", "别摸鱼", "别睡了", "写代码", "干活了",
            "摸尾巴", "摸摸头", "拍水花", "变胖了", "该减肥", "好圆啊", "咕嘟嘟",
        ],
        # 主动发言：默认关闭。群聊里主动说话是被风控关注的行为，交给用户显式开启。
        "proactive": {
            "enabled": False,
            "interval_hours": 2,
            "start_hour": 8,
            "end_hour": 20,
            "groups": [],
            "timeout": 8.0,
            "idle": {
                "enabled": False,
                "idle_minutes": 45,
                "cooldown_minutes": 120,
                "check_interval": 60,
                "min_messages": 0,
            },
        },
        # 收藏表情：每 N 次成功回复后附加一个（发不出就记欠账，不丢）
        "sticker": {
            "enabled": True,
            "reply_interval": 3,
            "min_interval": 600,
            "max_interval": 1200,
        },
        "image_search": {
            "enabled": False,
            "timeout": 3.0,
            "cache_dir": "./data/wechat-images",
        },
    },
    # 控制台「设置」页保存的键（与 wechat 段同步写入）
    "wechat_only": {
        "backend": "auto",
        # 指定机器人用哪个微信账号（账号目录名或 wxid）；留空=自动判断。
        # 为什么需要它：一台机器上可能同时登录两个微信号，这时"哪个账号在登录"没有
        # 权威信号，自动判断只能猜；猜错的表现是"连上了却收不到消息、发送也失败"。
        "account": "",
        "groups": [],
        "listen_private": True,
        "send_interval": 1.5,
        "auto_start": False,
    },
    "ai": {
        "api_url": "",
        "api_key_encrypted": "",
        "api_style": "auto",
        "model": "deepseek-chat",
        "max_tokens": 500,
        "temperature": 0.55,
        "timeout": 20,
        "max_retries": 2,
        "fast_reply_timeout": 1.2,
        "fast_reply_fallback": True,
    },
    # 本地语音识别（收到的语音转文字；不上传第三方语音接口）
    "asr": {
        "enabled": True,
        "model": "base",
        "model_dir": "./data/models/faster-whisper",
        "cache_dir": "./data/voice_inbox",
        "language": "zh",
        "compute_type": "int8",
        "cpu_threads": 4,
        "beam_size": 1,
        "max_audio_mb": 20.0,
        "hf_endpoint": "https://hf-mirror.com",
    },
    # 读图：本地 OCR 读出图片里的文字（图片不上传第三方）
    "image_reader": {
        "enabled": True,
        "ocr_timeout": 20.0,
        "max_image_mb": 12.0,
        "max_chars": 800,
    },
    "style": {
        "active": LOCKED_STYLE_NAME,
        "locked": False,
        "locked_name": None,
        "custom_styles_dir": "./styles/custom/",
    },
}

ENCRYPTED_FIELDS = {"ai.api_key_encrypted"}


def _derive_key(password: str, salt: bytes) -> bytes:
    """从用户密码派生 Fernet 密钥"""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


class ConfigManager:
    """配置管理器，支持 YAML 读写和敏感字段加密"""

    def __init__(self, config_path: str = "config.wechat.yaml"):
        self.config_path = Path(config_path)
        self._config: dict = {}
        self._fernet: Fernet | None = None
        self._salt: bytes = b"wechat_chatbot_salt_v1"
        self._recovered = False

    def load(self, password: str | None = None) -> dict:
        """加载配置文件，如果不存在则创建默认配置"""
        if password:
            self._fernet = Fernet(_derive_key(password, self._salt))

        if not self.config_path.exists():
            logger.warning(f"配置文件 {self.config_path} 不存在，使用默认配置")
            self._config = self._deep_copy(DEFAULT_CONFIG)
            return self._config

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            if loaded is None:
                raise ValueError('配置文件为空')
            if not isinstance(loaded,dict):
                raise ValueError('配置根节点必须是对象')
            self._config = self._merge(DEFAULT_CONFIG, loaded)
            self._enforce_style_lock()
            logger.info(f"配置文件加载成功: {self.config_path}")
        except Exception as e:
            logger.error(f"配置文件加载失败: {e}，使用默认配置")
            self._config = self._deep_copy(DEFAULT_CONFIG)
            backup=self.config_path.with_suffix(self.config_path.suffix+'.bak')
            try:
                loaded=yaml.safe_load(backup.read_text('utf-8'))
                if isinstance(loaded,dict):
                    self._config=self._merge(DEFAULT_CONFIG,loaded)
                    self._recovered=True
                    self._enforce_style_lock()
                    logger.warning('已使用上一份有效配置备份')
            except (OSError,ValueError,yaml.YAMLError):
                pass

        return self._config

    def save(self) -> None:
        """保存配置到文件"""
        try:
            self._enforce_style_lock()
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            from .persistence import atomic_text
            atomic_text(self.config_path,yaml.safe_dump(self._config,allow_unicode=True),backup=not self._recovered)
            self._recovered=False
            logger.info(f"配置已保存: {self.config_path}")
        except Exception as e:
            logger.error(f"配置保存失败: {e}")
            raise

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值，支持点号分隔的嵌套键"""
        keys = key.split(".")
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value

    def set(self, key: str, value: Any) -> None:
        """设置配置值，支持点号分隔的嵌套键"""
        keys = key.split(".")
        config = self._config
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        config[keys[-1]] = value

    def _enforce_style_lock(self) -> None:
        """只保证**默认人设存在**，不覆盖用户已选好的 active。"""
        style = self._config.setdefault("style", {})
        if not style.get("active"):
            style["active"] = LOCKED_STYLE_NAME
        style["locked"] = False
        style["locked_name"] = None

    def encrypt_value(self, plaintext: str) -> str:
        """加密字符串值。

        这里用**固定派生密钥**而不是用户输入的密码：本程序是单机自用工具，
        没有地方让用户每次启动输入密码。固定 salt + 固定口令只防"配置文件被顺手看到"，
        不防本地攻击者 —— 这一限制在文档里向用户写明。
        """
        if not self._fernet:
            self._fernet = Fernet(_derive_key("whalegirl-wechat-local", self._salt))
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt_value(self, encrypted: str) -> str:
        """解密字符串值"""
        if not encrypted:
            return ""
        if not self._fernet:
            self._fernet = Fernet(_derive_key("whalegirl-wechat-local", self._salt))
        try:
            return self._fernet.decrypt(encrypted.encode()).decode()
        except Exception as e:
            logger.error(f"解密失败: {e}")
            return ""

    def get_decrypted(self, key: str) -> str:
        """获取并解密配置值"""
        encrypted = self.get(key, "")
        return self.decrypt_value(encrypted) if encrypted else ""

    @property
    def config(self) -> dict:
        return self._config

    @staticmethod
    def _deep_copy(d: dict) -> dict:
        """简单深拷贝"""
        result = {}
        for k, v in d.items():
            if isinstance(v, dict):
                result[k] = ConfigManager._deep_copy(v)
            elif isinstance(v, list):
                result[k] = list(v)
            else:
                result[k] = v
        return result

    @staticmethod
    def _merge(default: dict, override: dict) -> dict:
        """递归合并配置，override 覆盖 default"""
        result = ConfigManager._deep_copy(default)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = ConfigManager._merge(result[key], value)
            else:
                result[key] = value
        return result
