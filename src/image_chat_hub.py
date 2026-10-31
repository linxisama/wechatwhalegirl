"""本机图片标注队列：复用识图配置，独立于微信外发和聊天模型锁。"""
import asyncio
import hashlib
import inspect
import time
from urllib.parse import urlsplit

from .image_chat_tags import annotation
from .sticker_library import ID, frames_for_vision


class ImageChatHub:
    def __init__(self, bot):
        self.bot, self.task = bot, None
        self.closed = False
        self.cancelling = False
        self.serial = asyncio.Lock()
        bot.db.execute('''CREATE TABLE IF NOT EXISTS imagechat_jobs(
            image_id TEXT PRIMARY KEY, state TEXT, origin TEXT, account TEXT,
            created REAL, updated REAL, error TEXT DEFAULT '')''')
        # 上次进程退出时请求可能已计费，不自动重做未知的请求。
        bot.db.execute("UPDATE imagechat_jobs SET state='failed',error='interrupted' WHERE state='running'")
        bot.db.commit()

    def enabled(self):
        p = self.bot.plugins._plugins.get('sticker_admin')
        return bool(p and p.enabled and self.bot.stickers is not None)

    def automatic(self):
        p = self.bot.plugins._plugins.get('sticker_admin')
        return bool(self.enabled() and p.get('autoRecognize', False))

    def settings(self):
        p = self.bot.plugins._plugins.get('image_understanding')
        if not p or not p.enabled:
            raise ValueError('请先启用图片与表情包识别')
        admin = self.bot.plugins._plugins['sticker_admin']
        endpoint = str(p.get('visionApiUrl', '') or '').strip()
        key = str(p.get('visionApiKey', '') or '').strip()
        model = str(p.get('visionModel', '') or '').strip() or (
            'deepseek-flash' if urlsplit(endpoint or self.bot.ai.api_url).hostname == 'api.deepseek.com'
            else self.bot.ai.model)
        return {'model': model, 'api_url': endpoint, 'api_key': key,
                'side': p._max_side(), 'frames': int(admin.get('gifFrames', 2)),
                'automatic': bool(admin.get('autoRecognize', False)),
                'timeout': max(3, min(120, float(p.get('visionTimeout', 25))))}

    def signature(self):
        return hashlib.sha256(repr((self.settings(), self.bot.ai.api_url,
            self.bot.ai.api_key, self.bot.account())).encode()).hexdigest()

    def status(self):
        counts = dict(self.bot.db.execute('SELECT state,COUNT(*) FROM imagechat_jobs GROUP BY state'))
        rows = self.bot.db.execute('SELECT image_id,state,origin,updated,error FROM imagechat_jobs '
                                   'ORDER BY updated DESC LIMIT 12').fetchall()
        return {'enabled': self.enabled(), 'automatic': self.automatic(),
                'counts': counts, 'capacity': 100,
                'items': [dict(zip(('id', 'state', 'origin', 'updated', 'error'), row)) for row in rows]}

    def enqueue(self, keys, origin='manual', retry=False):
        if self.closed or self.cancelling or not self.enabled():
            raise ValueError('图片标注服务已关闭')
        if origin == 'auto' and not self.automatic():
            return {'queued': 0, 'reused': 0, 'skipped': len(keys)}
        if not isinstance(keys, list) or not 1 <= len(keys) <= 20:
            raise ValueError('每批请选择1～20张图片')
        for key in keys:
            if not isinstance(key, str) or not ID.fullmatch(key):
                raise ValueError('图片标识无效')
            self.bot.stickers.get(key)
        queued = reused = skipped = 0
        for key in dict.fromkeys(keys):
            if not isinstance(key, str) or not ID.fullmatch(key):
                raise ValueError('图片标识无效')
            entry = self.bot.stickers.get(key)
            old = self.bot.db.execute('SELECT state FROM imagechat_jobs WHERE image_id=?', (key,)).fetchone()
            if old and (old[0] in ('pending', 'running') or (old[0] == 'failed' and not retry)):
                skipped += 1
                continue
            if entry.get('description'):
                if not entry.get('auto_tags'):
                    self.bot.stickers.annotate(key, annotation(entry['description']), source='local-reuse',
                        expected_revision=int(entry.get('_edit_revision') or 0), overwrite=True)
                reused += 1
                continue
            count = self.bot.db.execute("SELECT COUNT(*) FROM imagechat_jobs WHERE state IN ('pending','running')").fetchone()[0]
            if count >= 100:
                skipped += 1
                continue
            now = time.time()
            self.bot.db.execute('INSERT INTO imagechat_jobs VALUES(?,?,?,?,?,?,?) '
                'ON CONFLICT(image_id) DO UPDATE SET state=excluded.state,origin=excluded.origin,'
                'account=excluded.account,updated=excluded.updated,error=excluded.error',
                (key, 'pending', origin, self.bot.account(), now, now, ''))
            queued += 1
        self.bot.db.commit()
        self.start()
        return {'queued': queued, 'reused': reused, 'skipped': skipped}

    def start(self):
        if not self.closed and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.run())

    async def recognize(self, key, *, background=False):
        async with self.serial:
            if self.closed or not self.enabled():
                raise ValueError('图片标注服务已关闭')
            store = self.bot.stickers
            entry, cfg = store.get(key), self.settings()
            revision, signature = int(entry.get('_edit_revision') or 0), self.signature()
            if background and entry.get('description'):
                return False
            data = store.data(key)
            if hashlib.sha256(data).hexdigest() != key:
                raise ValueError('图片文件已改变，请重新上传')
            engine = self.bot.ai
            if background:
                # 独立客户端避免图库标注抢占聊天锁；仍共享原用量统计，不暗藏额外模型。
                from .ai_engine import AIEngine
                engine = AIEngine(engine.api_url, engine.api_key, model=engine.model,
                    timeout=cfg['timeout'], max_retries=1, proxy_url=engine.proxy_url)
                engine.usage = self.bot.ai.usage
            try:
                async with asyncio.timeout(cfg['timeout']):
                    frames = await asyncio.to_thread(frames_for_vision, data, cfg['frames'], cfg['side'])
                    options = {'model': cfg['model'], 'max_side': cfg['side']}
                    for field in ('api_url', 'api_key'):
                        if cfg[field]:
                            options[field] = cfg[field]
                    # 保留旧模型包装器只接受sources/model/max_side的兼容接口。
                    parameters = inspect.signature(engine.describe_images).parameters
                    if 'annotation' in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
                        options['annotation'] = True
                    if background:
                        text = await engine.describe_images(frames, **options)
                    else:
                        async with self.bot.ai_lock:
                            text = await engine.describe_images(frames, **options)
                    result = annotation(text)
                if self.closed or not self.enabled() or signature != self.signature():
                    raise ValueError('图片标注期间设置改变，未保存旧结果')
                return store.annotate(key, result, expected_revision=revision, overwrite=not background)
            finally:
                if background:
                    await engine.close()

    async def run(self):
        try:
            while not self.closed:
                # 留出消息准备的空隙，不在正在生成/发送回复时启动下一次标注。
                await asyncio.sleep(.5)
                if not self.enabled():
                    continue
                if self.bot.running and self.bot.pipeline.get('stage', 'idle') != 'idle':
                    continue
                if self.bot.running and self.bot.db.execute("SELECT 1 FROM inbox WHERE state IN ('pending','processing','sending') LIMIT 1").fetchone():
                    continue
                row = self.bot.db.execute("SELECT image_id,origin,account FROM imagechat_jobs WHERE state='pending' "
                                           'ORDER BY created LIMIT 1').fetchone()
                if row is None:
                    return
                key, origin, account = row
                if origin == 'auto' and not self.automatic():
                    self.finish(key, 'cancelled', 'automatic_disabled')
                    continue
                if account != self.bot.account():
                    self.finish(key, 'cancelled', 'account_changed')
                    continue
                self.finish(key, 'running')
                try:
                    saved = await self.recognize(key, background=True)
                    self.finish(key, 'done' if saved else 'skipped')
                except asyncio.CancelledError:
                    self.finish(key, 'cancelled' if self.cancelling else 'failed',
                                'cancelled' if self.cancelling else 'interrupted')
                    raise
                except KeyError:
                    self.finish(key, 'cancelled', 'deleted')
                except Exception as exc:
                    # 故障只影响这一张，不输出可能带凭据的异常正文，也不隔离聊天插件。
                    self.finish(key, 'failed', type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.bot.note('图库标注任务暂停：' + type(exc).__name__)

    def finish(self, key, state, error=''):
        self.bot.db.execute('UPDATE imagechat_jobs SET state=?,updated=?,error=? WHERE image_id=?',
                            (state, time.time(), error, key))
        self.bot.db.execute("DELETE FROM imagechat_jobs WHERE image_id IN (SELECT image_id FROM imagechat_jobs "
            "WHERE state NOT IN ('pending','running') ORDER BY updated DESC LIMIT -1 OFFSET 500)")
        self.bot.db.commit()

    async def cancel(self):
        self.cancelling = True
        task, self.task = self.task, None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.bot.db.execute("UPDATE imagechat_jobs SET state='cancelled',error='cancelled' WHERE state IN ('pending','running')")
        self.bot.db.commit()
        self.cancelling = False

    async def close(self):
        self.closed = True
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
