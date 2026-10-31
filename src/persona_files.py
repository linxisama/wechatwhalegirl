"""可携带可选形象图的人设文件，只访问宿主管理的图片目录。"""
import base64
import hashlib
import io
import json
import re
from pathlib import Path
import yaml
from PIL import Image
from .persistence import atomic_text
from .persona_import import parse_persona, import_name

MAX_FILE=5*1024*1024


def image_path(bot,name):
    if not re.fullmatch(r'[a-f0-9]{64}\.(png|jpg|webp|gif)',str(name)):
        raise ValueError('图片标识无效')
    return bot.root/'data/persona_images'/name


def save_image(bot,encoded):
    if not isinstance(encoded,str) or len(encoded)>3*1024*1024:
        raise ValueError('人设图片最大 2 MB')
    raw=base64.b64decode(encoded,validate=True)
    if not raw or len(raw)>2*1024*1024:
        raise ValueError('人设图片最大 2 MB')
    with Image.open(io.BytesIO(raw)) as img:
        ext={'PNG':'png','JPEG':'jpg','WEBP':'webp','GIF':'gif'}.get(img.format)
        if not ext or img.width*img.height>16_000_000:
            raise ValueError('请选择有效的 PNG/JPG/WebP/GIF，像素不超过 1600 万')
        img.verify()
    name=hashlib.sha256(raw).hexdigest()+'.'+ext
    path=image_path(bot,name)
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():
        # 内容寻址，上传中断只会留下未被人设引用的文件，不会改坏已有形象图。
        temp=path.with_suffix('.upload')
        temp.write_bytes(raw)
        temp.replace(path)
    return name


def export_persona(bot,name):
    style=bot.style.get_for_edit(name)
    if not style:
        raise ValueError('人设不存在')
    result={'format':'WhaleGirlPersona/v1','persona':style.to_dict()}
    if style.image:
        path=image_path(bot,style.image)
        if path.is_file():
            result['image_data']=base64.b64encode(path.read_bytes()).decode('ascii')
    return yaml.safe_dump(result,allow_unicode=True)


def import_details(bot, content, name='', filename='', auto_name=False):
    parsed = parse_persona(content, filename)
    persona = parsed['persona']
    persona['name'] = import_name(bot, persona, name, filename, auto_name)
    persona['image'] = ''
    existed = True
    if parsed['image_data']:
        raw = base64.b64decode(parsed['image_data'], validate=True)
        digest = hashlib.sha256(raw).hexdigest()
        existed = any(image_path(bot, digest+'.'+ext).exists() for ext in ('png','jpg','webp','gif'))
        persona['image'] = save_image(bot, parsed['image_data'])
    try:
        saved = bot.style.save_persona(persona)
    except Exception:
        # 图片和人设分别写盘；人设失败时只回收本次新增的图片，绝不删除已有图片。
        if persona['image'] and not existed:
            try:
                image_path(bot, persona['image']).unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return {'name': saved, 'display_name': persona['display_name'],
            'source_format': parsed['source_format'], 'prompt_chars': parsed['prompt_chars'],
            'warnings': parsed['warnings'], 'activated': False}


def import_persona(bot,content,name=''):
    return import_details(bot, content, name)['name']


def add_routes(app):
    from fastapi import Request,HTTPException
    from fastapi.responses import FileResponse

    @app.post('/api/persona-files/import')
    async def import_file(request:Request):
        try:
            body=await request.json()
            if not isinstance(body, dict):
                raise ValueError('导入请求必须是对象')
            if type(body.get('auto_name', False)) is not bool:
                raise ValueError('auto_name 必须是布尔值')
            async with app.state.bot.lock:
                result=import_details(app.state.bot,body.get('content'),str(body.get('name') or ''),
                                      str(body.get('filename') or ''), body.get('auto_name', False))
            return {'success':True,**result,'message':'人设已导入保存；点击「设为当前」才会用于回复'}
        except (ValueError,TypeError,yaml.YAMLError) as exc:
            raise HTTPException(400,str(exc)) from None
        except (OSError, Image.DecompressionBombError):
            raise HTTPException(400,'人设或内嵌图片无法保存，请检查图片格式、磁盘空间及文件权限；原有人设未覆盖') from None

    @app.get('/api/persona-files/{name}/export')
    async def export_file(name:str):
        try:
            return {'success':True,'content':export_persona(app.state.bot,name)}
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from None

    @app.post('/api/persona-files/image')
    async def upload_image(request:Request):
        body=await request.json()
        try:
            return {'success':True,'image':save_image(app.state.bot,body.get('data'))}
        except (ValueError,OSError,Image.DecompressionBombError) as exc:
            raise HTTPException(400,str(exc)) from None

    @app.get('/api/persona-images/{name}')
    async def get_image(name:str):
        try:
            path=image_path(app.state.bot,name)
            if not path.is_file():
                raise ValueError('图片不存在')
            return FileResponse(path)
        except ValueError as exc:
            raise HTTPException(404,str(exc)) from None

    @app.post('/api/persona-files/{name}/analyze')
    async def analyze_image(name:str):
        bot=app.state.bot
        style=bot.style.get_for_edit(name)
        if not style or not style.image:
            raise HTTPException(400,'请先保存带形象图的人设')
        path=image_path(bot,style.image)
        suffix=path.suffix[1:]
        mime='image/jpeg' if suffix=='jpg' else 'image/'+suffix
        source='data:'+mime+';base64,'+base64.b64encode(path.read_bytes()).decode('ascii')
        async with bot.ai_lock:
            description=await bot.ai.describe_images([source])
        if not description:
            raise HTTPException(400,bot.ai.last_error or '视觉接口未返回描述，可手工填写形象说明')
        data=style.to_dict()
        data.update(original_name=name,image_note=description[:2000])
        bot.style.save_persona(data)
        return {'success':True,'image_note':description[:2000],'message':'形象描述已保存，下一轮生效'}
