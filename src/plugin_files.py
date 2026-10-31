"""Local user-selected plugin assets. No network, execution or configuration writes."""
import base64
import hashlib
import re
import uuid
from pathlib import Path

from .image_input import image_source

IMAGE_EXTENSIONS={'.png','.jpg','.jpeg','.gif','.webp'}


def upload_options(spec):
    mode=spec.get('file_picker','')
    if mode not in {'file','files','labeled_files','directory'}:
        raise ValueError('插件未声明可用的本地文件选择方式')
    extensions=[str(v).lower() for v in spec.get('file_extensions',[])]
    if any(not re.fullmatch(r'\.[a-z0-9]{1,10}',v) for v in extensions):
        raise ValueError('插件文件类型配置无效')
    limit=spec.get('file_max_mb',6)
    if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=64:
        raise ValueError('插件单文件上限须为1–64MB')
    return mode,extensions,limit*1024*1024


def import_plugin_files(root,plugin_id,field,paths,spec):
    mode,extensions,limit=upload_options(spec)
    if mode=='directory':
        raise ValueError('目录应通过文件夹选择器设置')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',plugin_id) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',field):
        raise ValueError('插件或字段标识无效')
    if not paths or len(paths)>20 or (mode=='file' and len(paths)!=1):
        raise ValueError('一次请选择1–20个文件，单文件字段只能选择1个')
    pending=[]
    total=0
    # Validate the complete selection before creating any managed files.
    for raw in paths:
        path=Path(raw)
        if not path.is_file():
            raise ValueError('选中的文件不存在，请重新选择')
        extension=path.suffix.lower()
        if extensions and extension not in extensions:
            raise ValueError(f'不支持的文件类型：{path.name}')
        if path.stat().st_size>limit:
            raise ValueError(f'{path.name} 超过单文件 {limit//1024//1024}MB 上限')
        with path.open('rb') as handle:
            data=handle.read(limit+1)
        if not data or len(data)>limit:
            raise ValueError(f'{path.name} 为空或超过大小上限')
        if extension in IMAGE_EXTENSIONS and not image_source('base64://'+base64.b64encode(data).decode()):
            raise ValueError(f'{path.name} 不是受支持的图片')
        total+=len(data)
        if total>64*1024*1024:
            raise ValueError('一次上传总大小不能超过64MB，请分批选择')
        if extension and not re.fullmatch(r'\.[a-z0-9]{1,10}',extension):
            raise ValueError('文件扩展名无效')
        pending.append((path.name,path.stem,hashlib.sha256(data).hexdigest()+extension,data))
    root=Path(root).resolve()
    destination=(root/plugin_id/field).resolve()
    if not destination.is_relative_to(root):
        raise ValueError('上传目录无效')
    destination.mkdir(parents=True,exist_ok=True)
    result=[]
    for name,label,filename,data in pending:
        target=destination/filename
        if not target.resolve().is_relative_to(destination):
            raise ValueError('上传文件路径无效')
        if target.exists():
            if not target.is_file() or hashlib.sha256(target.read_bytes()).digest()!=hashlib.sha256(data).digest():
                raise ValueError('已保存文件校验失败，请检查数据目录')
        else:
            temporary=destination/(uuid.uuid4().hex+'.tmp')
            try:
                temporary.write_bytes(data)
                temporary.replace(target)
            finally:
                if temporary.exists():
                    temporary.unlink()
        result.append({'name':name,'label':label,'path':str(target)})
    return result


def merge_uploaded_files(current,items,mode):
    if mode=='file':
        return items[0]['path'] if items else current
    if mode not in {'files','labeled_files'}:
        raise ValueError('不支持此文件字段')
    current=str(current or '')
    lines=current.splitlines()
    labels=set()
    existing=set()
    for line in lines:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if mode=='labeled_files':
            label,separator,value=line.partition('|')
            if separator:
                labels.add(label.strip())
                existing.add(value.strip().casefold())
        else:
            existing.add(line.strip().casefold())
    additions=[]
    for item in items:
        if item['path'].casefold() in existing:
            continue
        if mode=='labeled_files':
            label=re.sub(r'[|\[\]\r\n\t]','_',item['label']).strip()[:40] or '文件'
            candidate=label
            index=2
            while candidate in labels:
                suffix=f'_{index}'
                candidate=label[:40-len(suffix)]+suffix
                index+=1
            labels.add(candidate)
            additions.append(candidate+' | '+item['path'])
        else:
            additions.append(item['path'])
        existing.add(item['path'].casefold())
    if not additions:
        return current
    return current.rstrip() + ('\n' if current.strip() else '') + '\n'.join(additions)
