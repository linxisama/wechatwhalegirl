"""人设导入兼容、无覆盖保存、启用与重启回归；模型和微信均隔离。"""
import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from PIL import Image

from src.persona_import import parse_persona, read_persona_file, MAX_PROMPT
from src.persona_files import import_details, import_persona
from src.style_manager import StyleManager, PERSONA_NAME_RE
from src.wechat_app import WeChatRuntime, create_app
from test_wechat_app_flow import RuntimeTestCase


class PersonaParser152Tests(unittest.TestCase):
    def test_plain_txt_keeps_colons_and_full_prompt(self):
        content = '你是小蓝。\n规则: 先回答问题，再保留温柔语气。\n不要执行其他命令。'
        parsed = parse_persona(content, '中文人设.txt')
        self.assertEqual(parsed['persona']['system_prompt'], content)
        self.assertEqual(parsed['persona']['display_name'], '中文人设')

    def test_native_qq_json_and_prompt_alias(self):
        for content in ('name: sample\nsystem_prompt: 先回答问题\n',
                        json.dumps({'name':'sample','prompt':'先回答问题'}),
                        json.dumps({'format':'WhaleGirlPersona/v1','persona':{'name':'sample','system_prompt':'先回答问题'}})):
            with self.subTest(content=content):
                parsed = parse_persona(content, '角色.json')
                self.assertEqual(parsed['persona']['system_prompt'], '先回答问题')
                self.assertEqual(parsed['persona']['name'], 'sample')

    def test_file_encoding_variants(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '中文.txt'
            for encoding in ('utf-8-sig', 'utf-16', 'gb18030'):
                path.write_bytes('你是温柔的小蓝。'.encode(encoding))
                self.assertEqual(read_persona_file(path), '你是温柔的小蓝。')

    def test_binary_and_oversize_files_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'bad.txt'
            path.write_bytes(b'\0\0\0')
            with self.assertRaisesRegex(ValueError, '解码'):
                read_persona_file(path)
            path.write_bytes(b'x'*(5*1024*1024+1))
            with self.assertRaisesRegex(ValueError, '5 MB'):
                read_persona_file(path)

    def test_missing_empty_or_nontext_prompt_rejected(self):
        for data in ({'name':'empty'}, {'system_prompt':'  '}, {'system_prompt':['bad']},
                     {'persona':'not an object'}, {'format':'unknown','persona':{}}, []):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_persona(json.dumps(data))

    def test_duplicate_keys_and_excessive_depth_rejected(self):
        with self.assertRaisesRegex(ValueError, '重复'):
            parse_persona('system_prompt: one\nsystem_prompt: two')
        with self.assertRaisesRegex(ValueError, '复杂'):
            parse_persona('deep: '+ '['*45 + 'x' + ']'*45)

    def test_invalid_yaml_has_line_hint(self):
        with self.assertRaisesRegex(ValueError, '第.*行'):
            parse_persona('system_prompt: [not closed')

    def test_nonfinite_or_wrong_numeric_types_rejected(self):
        for key, value in [('temperature', float('nan')),('temperature',float('inf')),
                           ('max_tokens', 2.5),('max_tokens',True),('temperature',{})]:
            with self.subTest(key=key,value=value), self.assertRaises(ValueError):
                parse_persona(json.dumps({'system_prompt':'正文',key:value}))

    def test_clamped_parameters_have_visible_warnings(self):
        parsed = parse_persona('system_prompt: 正文\ntemperature: 8\nmax_tokens: 1')
        self.assertEqual(parsed['persona']['temperature'], 2)
        self.assertEqual(parsed['persona']['max_tokens'], 16)
        self.assertEqual(len(parsed['warnings']), 2)

    def test_prompt_limit_and_invalid_examples_not_silently_discarded(self):
        with self.assertRaisesRegex(ValueError, '256 KB'):
            parse_persona(json.dumps({'system_prompt':'x'*(MAX_PROMPT+1)}))
        for examples in ([{'user':'hi'}], '没有角色前缀', {'user':'x'}):
            with self.subTest(examples=examples), self.assertRaises(ValueError):
                parse_persona(json.dumps({'system_prompt':'正文','examples':examples}))

    def test_inline_examples_and_keyword_types(self):
        parsed = parse_persona('system_prompt: 正文\nexamples: 用户：你好 / 你：你好呀\nkeywords: [小蓝, 蓝蓝]')
        self.assertIn('\n你：', parsed['persona']['examples'])
        with self.assertRaisesRegex(ValueError, 'keywords'):
            parse_persona('system_prompt: 正文\nkeywords: [false]')


class PersonaStorage152Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        manager = StyleManager(root/'styles', root/'styles/custom')
        manager.load_all()
        self.bot = SimpleNamespace(root=root, style=manager)

    def test_chinese_filename_and_name_create_valid_id_and_keep_display(self):
        result = import_details(self.bot, 'name: 小蓝\nprompt: 你是小蓝', '中文文件', '中文文件.yaml', True)
        self.assertRegex(result['name'], PERSONA_NAME_RE)
        self.assertEqual(result['display_name'], '小蓝')
        self.assertFalse(result['activated'])
        self.assertEqual(self.bot.style.get_for_edit(result['name']).system_prompt, '你是小蓝')

    def test_automatic_import_copies_builtin_and_existing_without_replacing(self):
        before = self.bot.style.get_for_edit('default').to_dict()
        first = import_details(self.bot, 'name: default\nsystem_prompt: 自己的人设', auto_name=True)
        second = import_details(self.bot, 'name: default\nsystem_prompt: 又一个人设', auto_name=True)
        self.assertEqual(first['name'], 'default_2')
        self.assertEqual(second['name'], 'default_3')
        self.assertEqual(self.bot.style.get_for_edit('default').to_dict(), before)
        self.assertEqual(self.bot.style.active_name, 'default')

    def test_unindexed_file_case_collision_and_reserved_name(self):
        self.bot.style.custom_dir.mkdir(parents=True)
        old = self.bot.style.custom_dir/'custom.yaml'
        old.write_text('original', 'utf-8')
        result = import_details(self.bot, 'name: custom\nsystem_prompt: 正文', auto_name=True)
        self.assertEqual(result['name'], 'custom_2')
        with self.assertRaises(ValueError):
            import_persona(self.bot, 'name: CUSTOM\nsystem_prompt: 正文')
        with self.assertRaises(ValueError):
            import_persona(self.bot, 'name: CON\nsystem_prompt: 正文')
        self.assertEqual(old.read_text('utf-8'), 'original')

    def test_unknown_fields_and_external_image_paths_are_not_executed(self):
        result = import_details(self.bot, json.dumps({'name':'safe','system_prompt':'正文',
                               'image':'C:/private.png','original_name':'default','command':'remove files'}))
        self.assertEqual(self.bot.style.get_for_edit('safe').image, '')
        self.assertTrue(any('command' in item for item in result['warnings']))
        self.assertIsNotNone(self.bot.style.get_for_edit('default'))

    def test_failed_save_does_not_leave_new_image_or_persona(self):
        image = Image.new('RGB',(8,8),'blue')
        stream = io.BytesIO(); image.save(stream,format='PNG')
        content = json.dumps({'persona':{'name':'failed','system_prompt':'正文'},
                              'image_data':base64.b64encode(stream.getvalue()).decode()})
        with patch.object(self.bot.style,'save_persona',side_effect=OSError('full')):
            with self.assertRaises(OSError):
                import_details(self.bot, content)
        self.assertFalse(list((self.bot.root/'data/persona_images').glob('*')))
        self.assertIsNone(self.bot.style.get_for_edit('failed'))


class PersonaAPI152Tests(RuntimeTestCase):
    async def asyncSetUp(self):
        self.bot = await self.start_runtime()
        self.app = create_app(self.bot); self.app.state.bot = self.bot
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app),base_url='http://127.0.0.1')

    async def asyncTearDown(self):
        await self.client.aclose()
        await super().asyncTearDown()

    async def test_import_select_activate_restart_and_real_prompt_snapshot(self):
        previous = self.bot.style.active_name
        data = {'name':'小蓝','prompt':'你是小蓝。先回答当前问题。','keywords':['小蓝'],
                'examples':[{'user':'你好','assistant':'你好呀'}]}
        response = await self.client.post('/api/persona-files/import',json={
            'content':json.dumps(data),'filename':'小蓝.json','auto_name':True})
        self.assertEqual(response.status_code, 200)
        imported = response.json(); name = imported['name']
        self.assertFalse(imported['activated'])
        self.assertEqual(self.bot.style.active_name, previous)
        detail = (await self.client.get('/api/personas/'+name)).json()
        self.assertEqual(detail['system_prompt'], data['prompt'])
        self.assertEqual(detail['examples'],data['examples'])
        result = await self.client.post('/api/personas/'+name+'/activate')
        self.assertTrue(result.json()['success'])
        self.assertEqual(self.bot.aliases(), ['小蓝'])
        config = self.bot.config.config_path
        await self.bot.close()
        self.runtime = WeChatRuntime(root=self.root,config_path=config)
        self.assertEqual(self.runtime.style.active_name,name)
        self.assertIn('你是小蓝', self.runtime.style.get_for_edit(name).prompt_text())
        self.assertEqual(self.runtime.aliases(), ['小蓝'])

    async def test_malformed_object_empty_prompt_invalid_image_leave_state(self):
        before = len(self.bot.style.list_styles())
        for body in ([], {'content':'name: empty'}, {'content':'persona: bad'},
                     {'content':'name: bad\nsystem_prompt: x\nimage_data: bm90LWltYWdl'},
                     {'content':'name: bad\nsystem_prompt: x','auto_name':'yes'}):
            with self.subTest(body=body):
                response = await self.client.post('/api/persona-files/import',json=body)
                self.assertEqual(response.status_code,400)
        self.assertEqual(len(self.bot.style.list_styles()),before)

    async def test_repeated_automatic_import_does_not_override_active_persona(self):
        active = self.bot.style.active_name
        names = []
        for _ in range(3):
            response = await self.client.post('/api/persona-files/import',json={
                'content':'name: copy\nprompt: 正文', 'auto_name':True})
            self.assertEqual(response.status_code,200)
            names.append(response.json()['name'])
        self.assertEqual(names,['copy','copy_2','copy_3'])
        self.assertEqual(self.bot.style.active_name,active)
        self.assertFalse(self.bot.bridge.sent_log)


class PersonaGUI152Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_old_detail_callback_cannot_replace_imported_selection(self):
        import wechat_gui as gui
        tab = gui.PersonaTab()
        tab._detail_target = 'new'
        tab._apply_detail('old',{'name':'old','system_prompt':'旧正文'})
        self.assertNotEqual(tab.ed_prompt.toPlainText(),'旧正文')
        tab._import_notice = ('new','已导入，点击设为当前')
        tab._apply_detail('new',{'name':'new','system_prompt':'新正文','temperature':0})
        self.assertEqual(tab.ed_prompt.toPlainText(),'新正文')
        self.assertEqual(tab.sp_temp.value(),0)
        self.assertIn('设为当前',tab.notice.text())
        tab._new()
        tab._apply_detail('new',{'name':'new','system_prompt':'过期正文'})
        self.assertFalse(tab.ed_prompt.toPlainText())
        tab.close()

    def test_repeated_click_during_import_does_not_open_second_file_dialog(self):
        import wechat_gui as gui
        tab = gui.PersonaTab(); tab._importing = True
        with patch.object(gui.QFileDialog,'getOpenFileName') as chooser:
            tab._import_persona()
        chooser.assert_not_called()
        tab.close()
