"""识图专用储藏区：只清理由本程序按内容命名的副本，不碰微信和会话原数据。"""
import hashlib
import os
from pathlib import Path
import re
import threading
import uuid

from .sticker_library import inspect_image,MAX_BYTES

MANAGED_NAME=re.compile(r'^[0-9a-f]{64}\.(?:png|jpg|gif|webp)$')


class ReceivedImageStore:
    LIMIT=50

    def __init__(self,root):
        self.root=Path(root).absolute()
        if self.root.is_symlink() or getattr(self.root,'is_junction',lambda:False)():
            raise ValueError('图片储藏区不能是链接目录')
        self.root.mkdir(parents=True,exist_ok=True)
        self._trusted=self.root.resolve()
        self._lock=threading.RLock()

    def save_and_read(self,data):
        # 识图拿到已落盘副本前，不允许另一条下载触发容量清理。
        with self._lock:
            result=self.put(data)
            return result,self.read(result['path'])

    def _check_root(self):
        if self.root.is_symlink() or self.root.resolve()!=self._trusted:
            raise ValueError('图片储藏区路径已变化，停止保存和清理')

    def _managed(self):
        self._check_root()
        files=[]
        for path in self.root.iterdir():
            if (MANAGED_NAME.fullmatch(path.name) and not path.is_symlink() and path.is_file()
                    and path.resolve().parent==self._trusted):
                files.append(path)
        return files

    def put(self,data):
        info=inspect_image(data)
        digest=hashlib.sha256(data).hexdigest()
        with self._lock:
            self._check_root()
            target=self.root/(digest+'.'+info['ext'])
            if target.is_symlink() or target.resolve().parent!=self._trusted:
                raise ValueError('图片副本路径不安全')
            temporary=self.root/('.incoming-'+uuid.uuid4().hex+'.tmp')
            try:
                with temporary.open('xb') as stream:stream.write(data)
                os.replace(temporary,target)
            finally:
                if temporary.exists():temporary.unlink()
            files=self._managed();deleted=0;failed=0
            intended=len(files)//2 if len(files)>self.LIMIT else 0
            # 刚保存的原图仍会被读取给模型，时钟调整也不能让它被当成“最旧”删除。
            victims=sorted((p for p in files if p!=target),key=lambda p:(p.stat().st_mtime_ns,p.name))[:intended]
            for path in victims:
                try:
                    if path.is_symlink() or path.resolve().parent!=self._trusted:
                        failed+=1;continue
                    path.unlink();deleted+=1
                except OSError:failed+=1
            count=len(self._managed())
            return dict(path=str(target),count=count,deleted=deleted,cleanup_failed=failed,
                        cleanup_requested=intended,bytes=len(data))

    def read(self,path):
        with self._lock:
            self._check_root()
            target=Path(path)
            if (not MANAGED_NAME.fullmatch(target.name) or target.is_symlink()
                    or target.resolve().parent!=self._trusted or not 0<target.stat().st_size<=MAX_BYTES):
                raise ValueError('不能读取储藏区以外的文件')
            raw=target.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=target.stem:
                raise ValueError('已保存图片校验失败')
            inspect_image(raw)
            return raw
