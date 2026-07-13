# -*- coding: utf-8 -*-
import os
import unittest

from hfport import gamedata as G
from hfport import porter as P
from hfport import swf as S
from hfport import abcpatch as A
from hfport.builder import Builder


BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
EYJA = os.path.join(BASE, 'HF Eyja 0.1.7.exe')
PMOM = os.path.join(BASE, 'Promised Mystery of Moisture V-1.9.6.exe')
PM20 = os.path.join(BASE, 'HF_Eyja_0.1.7 +PM20.exe')
HF07 = os.path.join(BASE, 'HeroFighter v0.7.exe')


def load_mod(path, name):
    _, swf_bytes = S.read_projector(path)
    view = G.ModView(S.SwfFile.parse(swf_bytes), name)
    view.load_all_lmis()
    return view


@unittest.skipUnless(os.path.exists(EYJA) and os.path.exists(PMOM), 'local game EXEs unavailable')
class LocalPortingRegressionTests(unittest.TestCase):
    def test_item_group_is_copied_even_when_int1_matches(self):
        src = load_mod(PMOM, 'pmom')
        dst = load_mod(EYJA, 'eyja')
        target_group_before = G.arr_items(dst.item.props['actionGroup'])[5]
        before = G.norm_obj_bytes(target_group_before)

        porter = P.Porter(src, dst, P.Plan(['lucas'], namespace='test'), log=lambda *_: None)
        porter.analyze()
        self.assertIn(5, porter.group_copy)
        self.assertNotIn(5, porter.group_map)
        self.assertNotEqual(porter.limb_map.get('item'), 'item')

        porter.transform()
        self.assertNotEqual(porter.group_map[5], 5)
        self.assertEqual(G.norm_obj_bytes(G.arr_items(porter.out_item.props['actionGroup'])[5]), before)
        source_item_lmi = src.lmis['lmi_data/item.lmi']
        source_wood = [
            source_item_lmi.pngs[i]
            for i, pic in enumerate(source_item_lmi.limb_pics)
            if pic.props.get('filename') == 'png_global/item/wood.png'
        ]
        mapped_wood = porter.png_map['png_global/item/wood.png']
        output_wood = [
            porter.out_itemlmi.pngs[i]
            for i, pic in enumerate(porter.out_itemlmi.limb_pics)
            if pic.props.get('filename') == mapped_wood
        ]
        self.assertEqual(G.md5(output_wood[-1]), G.md5(source_wood[-1]))

    def test_out_of_range_source_group_is_preserved_without_crash(self):
        src = load_mod(EYJA, 'eyja')
        dst = load_mod(PMOM, 'pmom')
        porter = P.Porter(src, dst, P.Plan(['raye'], namespace='test'), log=lambda *_: None)

        porter.analyze()
        porter.transform()

        self.assertIn(582, porter.invalid_group_map)
        final_groups = len(G.arr_items(porter.out_item.props['actionGroup']))
        self.assertGreaterEqual(porter.invalid_group_map[582], final_groups)


@unittest.skipUnless(
    os.path.exists(PM20) and os.path.exists(EYJA) and os.path.exists(HF07),
    'local extended EXE unavailable',
)
class ExtendedSourceRegressionTests(unittest.TestCase):
    def test_imported_character_collects_imported_ref_spt_root(self):
        src = load_mod(PM20, 'pm20')
        dst = load_mod(EYJA, 'eyja')
        porter = P.Porter(src, dst, P.Plan(['lucas_pm'], namespace='test'), log=lambda *_: None)

        porter.analyze()

        self.assertEqual(porter.port_ids[0], 'model0_pm')
        self.assertIn('lucas_pm', porter.port_ids)
        porter.transform()
        _, _, manifest = porter.payloads()
        spt_paths = [
            path for kind, _, path in manifest['load_sequence'] if kind == 'spt'
        ]
        self.assertLess(
            spt_paths.index('spt_data/model0_pm_test.spt'),
            spt_paths.index('spt_data/lucas_pm_test.spt'),
        )
        self.assertFalse(
            any('拓扑排序失败' in warning for warning in manifest['warnings'])
        )

    def test_private_page_num_limit_can_be_patched(self):
        _, swf_bytes = S.read_projector(PM20)
        swf = S.SwfFile.parse(swf_bytes)
        main = None
        for _, tag in swf.find_tags(S.TAG_DOABC):
            _, _, abc_bytes = S.parse_doabc(tag.body)
            abc = A.AbcFile.parse(abc_bytes)
            if abc.find_instance('Data', 'Global') is not None:
                main = abc
                break
        method, _ = main.find_class_method('Page', 'CharList', 'NextPage', static=False)
        body = main.body_of_method(method)

        self.assertEqual(Builder._read_next_page_limit(main, body), 3)
        self.assertEqual(Builder._patch_next_page_limit(main, body, 4), 1)
        self.assertEqual(Builder._read_next_page_limit(main, body), 4)

    def test_distinct_source_lmi_files_stay_distinct_and_ordered(self):
        src = load_mod(PM20, 'pm20')
        dst = load_mod(HF07, 'hf07')
        porter = P.Porter(
            src,
            dst,
            P.Plan(['taylor', 'taylor_pm'], namespace='test'),
            log=lambda *_: None,
        )

        porter.analyze()
        porter.transform()
        _, _, manifest = porter.payloads()

        original = porter.lmi_path_map['lmi_data/taylor.lmi']
        imported = porter.lmi_path_map['lmi_data/pm_taylor.lmi']
        self.assertNotEqual(original, imported)
        lmi_paths = [
            path for kind, _, path in manifest['load_sequence'] if kind == 'lmi'
        ]
        self.assertLess(lmi_paths.index(original), lmi_paths.index(imported))


if __name__ == '__main__':
    unittest.main()
