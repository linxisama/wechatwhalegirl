"""DSH 1.3.50 起 settings/describe 的 namespaces 从 dict 变成 list：两种形状都必须读出同一份提供商清单。

真机样本（DSH 1.3.50，llm-pi-ai.value.providers）直接写在用例里，避免用假数据掩盖形状不兼容。
"""
import unittest

from src.dsh_bridge import DshError, namespace_items, describe_catalog

MIKU_MODELS = ['deepseek-v4-pro', 'deepseek-v4.1-flash', 'glm-5.3-flash']
MI_MODELS = ['TM ·Auto', 'TM ·Fast', 'TM ·Deep', 'TM ·DeepSeek-V4', 'TM ·DeepSeek-V4.1',
             'TM ·GLM5.3', 'TM ·HY4', 'TM ·Kimi-K3', 'TM ·Kimi-K2.8', 'TM ·MiniMax-M3',
             'TM ·Space-Bunny']
ZU_MODELS = ['deepseek-v4.1-flash']
DSHAPI_MODELS = ['deepseek-v4-flash', 'deepseek-v4.1-flash', 'glm-5.3-flash', 'minimax-m3',
                 'deepseek-v4-pro', 'glm-5.2', 'glm-5.3', 'hy4', 'hy3', 'space-bunny',
                 'step-5-preview', 'minimax-m3.1-flash', 'mimo-v2.6-pro']


def models(ids):
    """DSH 真机里 models 是对象数组，不是字符串数组；用例照抄这个形状。"""
    return [{'id': mid, 'name': mid, 'contextWindow': 262144, 'maxTokens': 65536} for mid in ids]


def provider(ids, display=None):
    cfg = {'models': models(ids)}
    if display is not None:
        cfg['displayName'] = display
    return cfg


def pi_ai():
    return {'providers': {'miku': provider(MIKU_MODELS, '中转'), 'mi': provider(MI_MODELS, 'miku'),
                          'zu': provider(ZU_MODELS), 'dshapi': provider(DSHAPI_MODELS)}}


def namespace(ns, value):
    """真机一条命名空间项：ns + schema + value + 一堆无关键。"""
    return {'ns': ns, 'schema': {}, 'value': value, 'base': None, 'user': None,
            'autoGenerate': False, 'revision': 1}


def described_list(rows):
    return {'namespaces': [namespace(ns, value) for ns, value in rows]}


def described_dict(rows):
    return {'namespaces': dict(rows)}


REAL_ROWS = [('agent-default-model', {'provider': 'zu', 'model': 'deepseek-v4.1-flash'}),
             ('llm-pi-ai', pi_ai()),
             ('locale', {'lang': 'zh-CN'})]


class ListShapeTest(unittest.TestCase):
    def test_real_sample_reads_all_providers(self):
        rows, default = describe_catalog(described_list(REAL_ROWS))
        self.assertEqual([r['id'] for r in rows], ['miku', 'mi', 'zu', 'dshapi'])
        self.assertEqual([len(r['models']) for r in rows], [3, 11, 1, 13])
        zu = [r for r in rows if r['id'] == 'zu'][0]
        self.assertIn({'id': 'deepseek-v4.1-flash', 'name': 'deepseek-v4.1-flash'}, zu['models'])
        self.assertEqual(zu['displayName'], 'zu')
        self.assertEqual([r for r in rows if r['id'] == 'miku'][0]['displayName'], '中转')
        self.assertEqual(default, {'provider': 'zu', 'model': 'deepseek-v4.1-flash',
                                   'reasoningEffort': None})

    def test_default_keeps_three_keys(self):
        rows, default = describe_catalog(described_list(
            [('agent-default-model', {'provider': 'mi', 'model': 'TM ·Deep', 'reasoningEffort': 'high'})]))
        self.assertEqual(rows, [])
        self.assertEqual(default, {'provider': 'mi', 'model': 'TM ·Deep', 'reasoningEffort': 'high'})
        _, partial = describe_catalog(described_list([('agent-default-model', {'provider': 'zu'})]))
        self.assertEqual(sorted(partial), ['model', 'provider', 'reasoningEffort'])
        self.assertIsNone(partial['model'])
        self.assertIsNone(partial['reasoningEffort'])

    def test_non_dict_item_is_skipped(self):
        described = {'namespaces': [None, 'llm-broken', 42, ('llm-x', {}),
                                    namespace('llm-ok', {'providers': {'zu': provider(ZU_MODELS)}})]}
        rows, default = describe_catalog(described)
        self.assertEqual([r['id'] for r in rows], ['zu'])
        self.assertIsNone(default)

    def test_item_without_ns_is_skipped(self):
        described = {'namespaces': [{'value': {'providers': {'zu': provider(ZU_MODELS)}}},
                                    {'ns': None, 'value': {'providers': {'mi': provider(MI_MODELS)}}},
                                    {'ns': 'llm-ok', 'value': {'providers': {'zu': provider(ZU_MODELS)}}}]}
        rows, _ = describe_catalog(described)
        self.assertEqual([r['id'] for r in rows], ['zu'])

    def test_non_dict_value_is_skipped(self):
        described = described_list([('llm-list', ['zu']), ('llm-none', None),
                                    ('llm-ok', {'providers': {'zu': provider(ZU_MODELS)}})])
        rows, _ = describe_catalog(described)
        self.assertEqual([r['id'] for r in rows], ['zu'])

    def test_empty_providers_returns_empty_without_raising(self):
        rows, default = describe_catalog(described_list([('llm-ok', {'providers': {}})]))
        self.assertEqual(rows, [])
        self.assertIsNone(default)

    def test_same_provider_merges_across_namespaces(self):
        described = described_list([
            ('llm-a', {'providers': {'zu': provider(['m1', 'm2'], '主号')}}),
            ('llm-b', {'providers': {'zu': provider(['m2', 'm3'], '次号'), 'other': provider(['m1'])}})])
        rows, _ = describe_catalog(described)
        self.assertEqual([r['id'] for r in rows], ['zu', 'other'])
        zu = rows[0]
        self.assertEqual([m['id'] for m in zu['models']], ['m1', 'm2', 'm3'])
        self.assertEqual(zu['displayName'], '主号')
        self.assertEqual([m['id'] for m in rows[1]['models']], ['m1'])

    def test_unrecognized_shape_still_raises(self):
        for bad in ({'namespaces': 'llm-zu'}, {'namespaces': None}, {'namespaces': 7},
                    {}, None, []):
            with self.assertRaises(DshError):
                describe_catalog(bad)
        self.assertIsNone(namespace_items('oops'))


class DictShapeTest(unittest.TestCase):
    """旧形状的输出必须逐字不变：这里断言的是改动前就该得到的完整结果表。"""

    def test_legacy_dict_shape_exact_rows(self):
        described = described_dict([
            ('llm-pi-ai', {'providers': {'zu': {'displayName': ' 中转 ', 'models': [
                {'id': 'a', 'name': 'A'}, {'id': 'a', 'name': '重复'}, {'name': 'only-name'},
                {'id': ''}, 'not-a-dict']}}}),
            ('agent-default-model', {'provider': 'zu', 'model': 'a', 'reasoningEffort': 'max'}),
            ('other', {'providers': {'zu': provider(['x'])}})])
        rows, default = describe_catalog(described)
        self.assertEqual(rows, [{'id': 'zu', 'displayName': '中转',
                                 'models': [{'id': 'a', 'name': 'A'},
                                            {'id': 'only-name', 'name': 'only-name'}]}])
        self.assertEqual(default, {'provider': 'zu', 'model': 'a', 'reasoningEffort': 'max'})

    def test_legacy_non_dict_provider_cfg_keeps_old_result(self):
        rows, _ = describe_catalog({'namespaces': {'llm-x': {'providers': {'p': 'oops', '': provider(['a'])}}}})
        self.assertEqual(rows, [{'id': 'p', 'displayName': 'p', 'models': []}])

    def test_legacy_and_list_shapes_agree(self):
        self.assertEqual(describe_catalog(described_dict(REAL_ROWS)),
                         describe_catalog(described_list(REAL_ROWS)))

    def test_legacy_default_not_dict_is_none(self):
        _, default = describe_catalog({'namespaces': {'agent-default-model': 'zu'}})
        self.assertIsNone(default)


if __name__ == '__main__':
    unittest.main()
