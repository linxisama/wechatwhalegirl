"""1.3.90-C 模板比对模块的验收测试：渲染、剪枝、排序、裁决、真机夹具回归、全池性能。

夹具是真机截下来的标题墨迹（.tmp-ink*.png，仓库根目录），不是合成的：
合成图只能证明算法自洽，真机图才能证明它在反锯齿、缩放、字重不一致时还认得出。
"""
import os
import random
import sys
import time
import unittest

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.wechat_title_template import (  # noqa: E402
    ASPECT_TOLERANCE, FONT_PATH, MIN_MARGIN, MIN_SCORE, PREFILTER_MARGIN, _quick_aspect,
    ink_box, judge, rank, render_template, score_against,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_192 = os.path.join(REPO_ROOT, '.tmp-ink192.png')      # 真值 'bilibili粉丝'
FIXTURE_191 = os.path.join(REPO_ROOT, '.tmp-ink-191.png')     # 真值 '李清浩'
FIXTURE_190B = os.path.join(REPO_ROOT, '.tmp-ink-190b.png')   # 真值 '摆鑫学长'


def load(path):
    """夹具是 RGB PNG；模块要的是内存图，不给路径（路径会抛 TypeError）。"""
    with Image.open(path) as image:
        return image.convert('L')


def synthetic_title(text, size=18, pad=6):
    """按真机标题栏的字号造一张合成标题图：18px 才是要验的那个尺寸。"""
    font = ImageFont.truetype(FONT_PATH, size)
    width = int(round(font.getlength(text))) + 2 * pad
    canvas = Image.new('L', (width, size * 2), 255)
    ImageDraw.Draw(canvas).text((pad, pad), text, fill=0, font=font)
    return canvas


def fake_pool(count=5600, seed=20261106):
    """造一个「像真名」的池：长度 2-8 字、常用姓氏 + 常用名。

    早期基准用过 '测试候选名字%d' 这种同形名，5600 个全被宽高比剪枝，
    跑出 0.00s 的假成绩；池必须有多样的宽高比，性能数字才有意义。
    """
    rng = random.Random(seed)
    surnames = ('王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董'
                '袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦')
    given = ('伟芳娜秀英敏静丽强磊军洋勇艳杰娟涛明超霞平刚桂建华文辉红玉兰春梅志远晓东晨'
             '光雨辰欣怡子涵浩然思远嘉雨轩梓萱语彤天宇博思琪若曦')
    names = set()
    while len(names) < count:
        length = rng.choice((2, 3, 3, 3, 4, 4, 5, 6, 7, 8))
        names.add(rng.choice(surnames) + ''.join(rng.choice(given) for _ in range(length - 1)))
    return sorted(names)


class RenderTemplateTest(unittest.TestCase):
    """A1(a)(b)：渲染出墨迹、空文本返回 None、结果进缓存。"""

    def test_normal_chinese_name_has_ink(self):
        result = render_template('李清浩')
        self.assertIsNotNone(result)
        mask, box = result
        self.assertIsInstance(mask, np.ndarray)
        self.assertEqual(mask.dtype, np.bool_)
        self.assertGreater(int(mask.sum()), 0, 'mask 里必须真有墨迹像素')
        self.assertEqual(mask.shape, (box[3] - box[1], box[2] - box[0]),
                         '掩膜尺寸必须与紧致外包框一致')

    def test_blank_text_returns_none(self):
        # 池里真有渲染不出一个墨迹像素的名字；不返回 None 就会在取 min/max 时抛
        # 'zero-size array to reduction operation minimum which has no identity'。
        for text in ('   ', '', '\u3000\u3000'):
            with self.subTest(text=repr(text)):
                self.assertIsNone(render_template(text))

    def test_render_is_cached(self):
        before = render_template.cache_info()
        first = render_template('摆鑫学长')
        second = render_template('摆鑫学长')
        after = render_template.cache_info()
        self.assertIs(first, second, '缓存命中必须返回同一个对象，不能重渲染')
        self.assertGreater(after.hits, before.hits, 'cache_info 的 hits 必须增长')
        self.assertFalse(first[0].flags.writeable, '共享的缓存掩膜必须锁死写权限')

    def test_render_result_is_deterministic(self):
        render_template.cache_clear()
        first = render_template('极客先锋社招新群（77）')
        render_template.cache_clear()
        second = render_template('极客先锋社招新群（77）')
        self.assertEqual(first[1], second[1])
        self.assertTrue(np.array_equal(first[0], second[0]))


class RankTest(unittest.TestCase):
    """A1(c)(d)(f)：空输入、合成 18px 标题图、宽高比剪枝。"""

    def test_empty_names_returns_empty(self):
        self.assertEqual(rank(load(FIXTURE_191), []), [])
        self.assertEqual(rank(load(FIXTURE_191), ()), [])

    def test_no_ink_image_returns_empty(self):
        self.assertEqual(rank(Image.new('L', (30, 20), 255), ['李清浩']), [])

    def test_rank_synthetic_18px_title(self):
        image = synthetic_title('极客先锋社招新群（77）')
        pool = ['极客先锋社招新群（77）', '极客先锋社招新群', '一', '人工智能课交流群（20）']
        scores = rank(image, pool)
        self.assertEqual(len(scores), len(pool))
        self.assertEqual(scores[0][1], '极客先锋社招新群（77）', scores)
        # 分数降序，且真值要有真实领先幅度，不能只是并列里排前面。
        self.assertEqual([s for s, _ in scores], sorted((s for s, _ in scores), reverse=True))
        self.assertGreaterEqual(scores[0][0], MIN_SCORE)
        self.assertGreaterEqual(scores[0][0] - scores[1][0], MIN_MARGIN)

    def test_aspect_pruning_rejects_very_wide_name(self):
        image = load(FIXTURE_191)
        ink_mask, ink_size = ink_box(image)
        wide = '一' * 40
        # '一'*40 在 32px 下是 1280x3，宽高比 426；真机墨迹是 52/17≈3.06，差了两个数量级。
        self.assertEqual(score_against(ink_mask, ink_size, render_template(wide)), 0.0)
        scores = dict((name, score) for score, name in rank(image, [wide, '李清浩']))
        self.assertEqual(scores[wide], 0.0)
        self.assertLess(scores[wide], MIN_SCORE)
        self.assertEqual(rank(image, [wide, '李清浩'])[0][1], '李清浩')

    def test_tolerance_is_applied(self):
        image = load(FIXTURE_191)
        ink_mask, ink_size = ink_box(image)
        template = render_template('李清浩')
        strict = score_against(ink_mask, ink_size, template, tolerance=0.001)
        self.assertEqual(strict, 0.0, '容差收紧到 0.001 时同名也必须被剪掉')


class JudgeTest(unittest.TestCase):
    """A1(e)：judge() 的五个 reason 分支各一例。"""

    def test_accepted(self):
        verdict = judge(load(FIXTURE_190B), '摆鑫学长', ['摆鑫学长', '摆鑫学', '李'])
        self.assertEqual(set(verdict), {'accepted', 'mine', 'best_rival', 'rival', 'margin', 'reason'})
        self.assertTrue(verdict['accepted'])
        self.assertEqual(verdict['reason'], 'accepted')
        self.assertGreaterEqual(verdict['mine'], MIN_SCORE)
        self.assertGreaterEqual(verdict['margin'], MIN_MARGIN)

    def test_below_threshold(self):
        # 渲染得出、但形状对不上：分数低于接受线，先修的是模板本身。
        verdict = judge(load(FIXTURE_190B), '摆鑫学', ['摆鑫学', '李'])
        self.assertFalse(verdict['accepted'])
        self.assertEqual(verdict['reason'], 'below_threshold')
        self.assertLess(verdict['mine'], MIN_SCORE)

    def test_margin_too_small(self):
        # 池里有一个与 mine 渲染完全同形的名字（尾随空格不产生墨迹），领先幅度为 0。
        verdict = judge(load(FIXTURE_191), '李清浩', ['李清浩', '李清浩 '])
        self.assertFalse(verdict['accepted'])
        self.assertEqual(verdict['reason'], 'margin_too_small')
        self.assertGreaterEqual(verdict['mine'], MIN_SCORE)
        self.assertLess(verdict['margin'], MIN_MARGIN)

    def test_not_in_pool(self):
        verdict = judge(load(FIXTURE_191), '李清浩', ['摆鑫学长'])
        self.assertFalse(verdict['accepted'])
        self.assertEqual(verdict['reason'], 'not_in_pool')
        self.assertEqual(verdict['mine'], 0.0)

    def test_no_ink(self):
        verdict = judge(Image.new('L', (30, 20), 255), '李清浩', ['李清浩'])
        self.assertFalse(verdict['accepted'])
        self.assertEqual(verdict['reason'], 'no_ink')
        self.assertEqual(verdict['mine'], 0.0)


class RealFixtureRegressionTest(unittest.TestCase):
    """A2：三帧真机墨迹的回归——这三帧是这套算法唯一的证据来源。"""

    def check(self, path, truth, pool):
        image = load(path)
        started = time.perf_counter()
        scores = rank(image, pool)
        elapsed = (time.perf_counter() - started) * 1000
        top3 = ' / '.join('%.3f %s' % (score, name) for score, name in scores[:3])
        print('\n  %s 真值=%s %.1fms top3: %s' % (os.path.basename(path), truth, elapsed, top3))
        self.assertTrue(scores, '%s 必须至少出一个分' % path)
        self.assertEqual(scores[0][1], truth, '%s top1 应为 %r，实际 top3: %s' % (path, truth, top3))
        self.assertLess(elapsed, 3000.0)
        return scores

    def test_fixture_bilibili(self):
        # 池里放的是 OCR 读出来的邻形 'bilibli粉丝'（少一个 i），真值 'bilibili粉丝'
        # 反而不在池里：这帧验的是「按形状能把 OCR 的错字挑出来」，不是认出原名。
        pool = ['bilibli粉丝', 'bilibili粉丝', 'bilibli 粉丝', 'bilibiIi粉丝']
        scores = self.check(FIXTURE_192, 'bilibli粉丝', pool)
        self.assertGreaterEqual(scores[0][0], MIN_SCORE)
        self.assertGreaterEqual(scores[0][0] - scores[1][0], MIN_MARGIN)

    def test_fixture_liqinghao(self):
        pool = ['李清浩', '李清浩。', '李清浩2', '李消浩']
        scores = self.check(FIXTURE_191, '李清浩', pool)
        self.assertGreaterEqual(scores[0][0], MIN_SCORE)

    def test_fixture_baixin(self):
        pool = ['摆鑫学长', '摆鑫学', '摆鑫学长。', '摆鑫学艮']
        scores = self.check(FIXTURE_190B, '摆鑫学长', pool)
        self.assertGreaterEqual(scores[0][0], MIN_SCORE)


class PrefilterTest(unittest.TestCase):
    """渲染前的 getbbox 粗筛只能放宽、不能改分：剪掉的名字本来就该是 0 分。"""

    def test_prefilter_does_not_change_any_score(self):
        pool = fake_pool(5600)[::7]      # 均匀抽样，覆盖各种长度与宽高比
        self.assertGreater(len(pool), 700)
        for fixture in (FIXTURE_192, FIXTURE_191, FIXTURE_190B):
            with self.subTest(fixture=os.path.basename(fixture)):
                image = load(fixture)
                ink_mask, ink_size = ink_box(image)
                reference = []
                for name in pool:
                    template = render_template(name)
                    reference.append(0.0 if template is None else
                                     score_against(ink_mask, ink_size, template))
                scored = {name: score for score, name in rank(image, pool)}
                self.assertEqual(set(scored), set(pool), 'rank 必须每个名字都出一个分')
                for name, expected in zip(pool, reference):
                    self.assertAlmostEqual(scored[name], expected, places=12,
                                           msg='%s 的得分被粗筛改掉了' % name)

    def test_prefilter_keeps_every_name_the_exact_prune_keeps(self):
        pool = fake_pool(5600)[::7]
        for fixture in (FIXTURE_192, FIXTURE_191, FIXTURE_190B):
            with self.subTest(fixture=os.path.basename(fixture)):
                ink_mask, ink_size = ink_box(load(fixture))
                for name in pool:
                    template = render_template(name)
                    if template is None:
                        continue
                    exact = score_against(ink_mask, ink_size, template)
                    if exact <= 0.0:
                        continue
                    # 精确剪枝放行的名字，粗筛必须也放行——否则 rank 会把它判成 0 分。
                    quick = _quick_aspect(name)
                    self.assertIsNotNone(quick, '%s 有墨迹，getbbox 不该返回 None' % name)
                    ink_aspect = ((ink_size[2] - ink_size[0])
                                  / (ink_size[3] - ink_size[1]))
                    self.assertLessEqual(abs(quick - ink_aspect) / ink_aspect,
                                         ASPECT_TOLERANCE + PREFILTER_MARGIN,
                                         '%s 被粗筛误杀' % name)


class PerformanceTest(unittest.TestCase):
    """A3：5600 个候选名的耗时上限 3.0 秒。"""

    def test_rank_5600_names_under_three_seconds(self):
        names = fake_pool(5600)
        self.assertEqual(len(names), 5600)
        image = load(FIXTURE_190B)
        render_template.cache_clear()   # 冷启动才是真机第一次调用的样子
        started = time.perf_counter()
        scores = rank(image, names)
        cold = time.perf_counter() - started
        started = time.perf_counter()
        rank(image, names)
        warm = time.perf_counter() - started
        print('\n  5600 候选：冷 %.2fs / 缓存后 %.2fs，top1 %.3f %s'
              % (cold, warm, scores[0][0], scores[0][1]))
        self.assertEqual(len(scores), 5600)
        self.assertLess(cold, 3.0, '冷启动 %.2fs 超过 3.0s 上限' % cold)
        self.assertLess(warm, cold)


class PoolScaleTest(unittest.TestCase):
    """全池规模下的行为：5600 个真名里可能有与真值同形的邻形名，会把领先幅度吃掉。"""

    def test_true_name_wins_against_5600_names(self):
        names = fake_pool(5600)
        cases = ((FIXTURE_192, 'bilibli粉丝'), (FIXTURE_191, '李清浩'),
                 (FIXTURE_190B, '摆鑫学长'))
        for path, mine in cases:
            with self.subTest(fixture=os.path.basename(path)):
                verdict = judge(load(path), mine, [mine] + names)
                print('\n  %s 池 5600：%s mine=%.3f rival=%.3f(%s) margin=%.3f'
                      % (os.path.basename(path), verdict['reason'], verdict['mine'],
                         verdict['best_rival'], verdict['rival'], verdict['margin']))
                self.assertGreaterEqual(verdict['mine'], MIN_SCORE)
                self.assertGreater(verdict['mine'], verdict['best_rival'],
                                   '全池里不能有名字得分超过真值')
                self.assertNotEqual(verdict['reason'], 'not_in_pool')


if __name__ == '__main__':
    unittest.main()
