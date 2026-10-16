# -*- coding: utf-8 -*-
"""跨版本移植的通用回归（不绑定某一对 EXE 的具体 bug）。

原来这个文件针对的是 HF Eyja 0.1.7 / PMoM 1.9.6 / PM20 / HeroFighter v0.7，
那几个文件已经不在工作目录里，于是 5 个用例全程 skip —— 被跳过的用例不是防线。
现在改用目录里确实存在的 EXE，并顺手把两条以前没覆盖的路径纳进来：

  * **源带 Pt 模板** 的方向（HFEP 有 149 个模板）。之前只测了 HF-EX（0 个模板），
    也就是只跑过 invalid-ref 那条分支。
  * **源本身就是移植产物** 的层叠移植（+port22 里的 *_ex 角色，refSpt 指向 model0_ex）。
"""
import os
import unittest

from hfport import abcpatch as A
from hfport import amf as M
from hfport import gamedata as G
from hfport import porter as P
from hfport import swf as S
from hfport.builder import Builder

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
HFEX = os.path.join(BASE, 'HF-EX V 0.2.5.exe')
HFEP = os.path.join(BASE, 'HFEP 正式版V2.7.exe')
EYJA = os.path.join(BASE, 'HF Eyja 0.1.8(爱丽丝·利德尔) .exe')
PORT22 = os.path.join(BASE, 'HF_Eyja_0.1.8_ +port22.exe')

_CACHE = {}


def load_mod(path, name):
    if path not in _CACHE:
        _, swf_bytes = S.read_projector(path)
        view = G.ModView(S.SwfFile.parse(swf_bytes), name)
        view.load_all_lmis()
        _CACHE[path] = view
    return _CACHE[path]


def cre_of(spt):
    for fr in G.arr_items(spt.props.get('frame')):
        if not isinstance(fr, M.AmfObj):
            continue
        for ctx, pt in G.frame_pts(fr):
            if ctx == 'cre':
                yield pt


@unittest.skipUnless(os.path.exists(HFEP) and os.path.exists(HFEX),
                     'HFEP / HF-EX EXE 不在工作目录')
class TemplateBearingSourceTests(unittest.TestCase):
    """源有 Pt 模板、目标没有 —— 不内联模板结果就会丢掉速度/朝向。

    HFEP 有 149 个模板，HF-EX 有 0 个。把 skadi 从 HFEP 移到 HF-EX，如果还留着
    ref>=0，目标里 CheckCopyRefPt 的范围检查会直接跳过覆盖，cre 上那份没被覆盖过的
    原始 vx/int1 就成了实际生效值 —— 和源里的表现不同。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = load_mod(HFEP, 'hfep')
        cls.dst = load_mod(HFEX, 'hfex')
        cls.porter = P.Porter(cls.src, cls.dst,
                              P.Plan(['skadi'], namespace='tpl'),
                              log=lambda *a: None)
        cls.porter.analyze()
        cls.porter.transform()

    def test_source_and_target_have_different_pt_table_sizes(self):
        self.assertEqual(len(self.dst.gdat.pts), 0)
        self.assertGreater(len(self.src.gdat.pts), 100)

    def test_template_path_is_actually_exercised(self):
        stat = {}
        for pt in cre_of(self.src.spt('skadi')):
            res = G.resolve_pt(self.src, pt, 'cre')
            stat[res.status] = stat.get(res.status, 0) + 1
        self.assertGreater(stat.get('template', 0), 0,
                           '这个源没有走模板分支的 cre，用例失去意义: %r' % stat)
        self.assertGreater(stat.get('direct', 0), 0)

    def test_ported_cre_inlines_the_source_resolved_values(self):
        src_resolved = []
        for pt in cre_of(self.src.spt('skadi')):
            res = G.resolve_pt(self.src, pt, 'cre')
            src_resolved.append(res)
        out = list(cre_of(self.porter.out_spts['skadi']))
        self.assertEqual(len(out), len(src_resolved))
        for res, pt in zip(src_resolved, out):
            self.assertEqual(G.ival(pt.props.get('ref'), -1), -1)
            self.assertIsNone(pt.props.get('refName'))
            for key in ('vx', 'vy', 'vz', 'facing', 'int2', 'int3'):
                self.assertEqual(
                    pt.props.get(key), res.final.props.get(key),
                    '%s 没有内联源解析结果' % key)

    def test_ported_groups_are_local_to_the_routed_pack(self):
        """独立包的组下标从 0 密排，且目标 itemSpt 一个字节都不动。"""
        out_groups = G.arr_items(self.porter.out_item.props['actionGroup'])
        src_groups = G.arr_items(self.src.item.props['actionGroup'])
        self.assertEqual(len(out_groups), len(src_groups))
        for src_i, out_i in self.porter.group_map.items():
            self.assertEqual(src_i, out_i, '组号必须是恒等映射')
        for pt in cre_of(self.porter.out_spts['skadi']):
            i1 = G.ival(pt.props.get('int1'), -1)
            if i1 >= 0:
                self.assertLess(i1, len(out_groups))
                self.assertEqual(pt.props.get('itemSptRoute'), 'tpl')
        _, rep_p, _ = self.porter.payloads()
        self.assertNotIn('Data.Global_itemSpt', rep_p)


@unittest.skipUnless(os.path.exists(PORT22) and os.path.exists(EYJA),
                     '+port22 / 干净 Eyja EXE 不在工作目录')
class LayeredRePortTests(unittest.TestCase):
    """源本身是移植产物：*_ex 角色的 refSpt 指向 model0_ex，必须一起带过去。"""

    @classmethod
    def setUpClass(cls):
        cls.src = load_mod(PORT22, 'port22')
        cls.dst = load_mod(EYJA, 'eyja')
        cls.porter = P.Porter(cls.src, cls.dst,
                              P.Plan(['lucas_ex'], namespace='again'),
                              log=lambda *a: None)
        cls.porter.analyze()

    def test_imported_character_drags_its_imported_ref_spt_root(self):
        self.assertIn('lucas_ex', self.porter.port_ids)
        root = self.src.spt('lucas_ex').props.get('refSpt')
        self.assertTrue(root and root != 'model0',
                        '这个源角色的 refSpt 不是移植来的，用例失去意义: %r' % root)
        self.assertIn(root, self.porter.port_ids)
        self.assertLess(self.porter.port_ids.index(root),
                        self.porter.port_ids.index('lucas_ex'),
                        '依赖必须排在使用者之前')

    def test_load_sequence_puts_the_ref_spt_first(self):
        self.porter.transform()
        _, _, manifest = self.porter.payloads()
        spt_paths = [p for k, _, p in manifest['load_sequence'] if k == 'spt']
        root = self.src.spt('lucas_ex').props.get('refSpt')
        root_path = 'spt_data/%s.spt' % self.porter.plan.new_id(root)
        user_path = 'spt_data/%s.spt' % self.porter.plan.new_id('lucas_ex')
        self.assertIn(root_path, spt_paths)
        self.assertLess(spt_paths.index(root_path), spt_paths.index(user_path))
        self.assertFalse(
            [w for w in manifest['warnings'] if '拓扑排序失败' in w])


@unittest.skipUnless(os.path.exists(PORT22), '+port22 EXE 不在工作目录')
class CharListPageLimitTests(unittest.TestCase):
    """角色一览翻页上限是私有语义操作数，只能按指令形状定位。"""

    def test_page_limit_can_be_read_and_patched_in_place(self):
        _, swf_bytes = S.read_projector(PORT22)
        swf = S.SwfFile.parse(swf_bytes)
        main = None
        for _, tag in swf.find_tags(S.TAG_DOABC):
            _, _, abc_bytes = S.parse_doabc(tag.body)
            abc = A.AbcFile.parse(abc_bytes)
            if abc.find_instance('Data', 'Global') is not None:
                main = abc
                break
        method, _ = main.find_class_method('Page', 'CharList', 'NextPage',
                                          static=False)
        body = main.body_of_method(method)
        before = Builder._read_next_page_limit(main, body)
        length_before = len(body.code)
        self.assertGreaterEqual(before, 1)
        self.assertEqual(Builder._patch_next_page_limit(main, body, before + 1), 1)
        self.assertEqual(Builder._read_next_page_limit(main, body), before + 1)
        # 只换 pushbyte 操作数，字节长度必须不变，否则后面所有跳转都会平移。
        self.assertEqual(len(body.code), length_before)


@unittest.skipUnless(os.path.exists(PORT22) and os.path.exists(HFEP),
                     '+port22 / HFEP EXE 不在工作目录')
class LmiBucketingTests(unittest.TestCase):
    """不同源 LMI 文件不能被合并，且加载顺序要跟着角色首次引用顺序。"""

    def test_distinct_source_lmi_files_stay_distinct_and_ordered(self):
        src = load_mod(PORT22, 'port22')
        dst = load_mod(HFEP, 'hfep')
        porter = P.Porter(src, dst, P.Plan(['lucas', 'lucas_ex'],
                                           namespace='buck'),
                          log=lambda *a: None)
        porter.analyze()
        porter.transform()
        _, _, manifest = porter.payloads()
        source_paths = [p for p in porter.lmi_path_map
                        if p != 'lmi_data/item.lmi']
        self.assertGreater(len(source_paths), 1)
        mapped = [porter.lmi_path_map[p] for p in source_paths]
        self.assertEqual(len(set(mapped)), len(mapped),
                         '两个不同的源 LMI 被映射到了同一个输出文件')
        lmi_paths = [p for k, _, p in manifest['load_sequence']
                     if k in ('lmi', 'early_lmi')]
        for path in mapped:
            self.assertIn(path, lmi_paths)


if __name__ == '__main__':
    unittest.main()
