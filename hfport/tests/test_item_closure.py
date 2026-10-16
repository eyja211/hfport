# -*- coding: utf-8 -*-
"""纯单元测试：cre 的 Pt 解析语义、ABC 重发射原语。不需要任何 EXE。

这些断言编码的是"为什么"，别随手放宽：
  * resolve_pt 必须和 Data.Spt.CheckCopyRefPt 一字不差 —— 先按 refName 查
    Global.ptHash 改写 ref，再在 ref 落在 Global.pt 范围内时用模板覆盖
    facing/vx/vy/vz/int1/int2/int3。整个 item bug 就是这个语义没被复现。
  * rewrite_code 的空替换（删除）和插入必须把每一条分支重定位对。
"""
import unittest

from hfport import abcpatch as A
from hfport import amf as M
from hfport import gamedata as G


def mkpt(**props):
    base = {'l': 1.0, 'j': 0.0, 'sx': 0.0, 'sy': 0.0, 'x': 0.0, 'y': 0.0,
            'z': 0.0, 'o': 0.0, 'ai': 0.0, 'facing': 0.0, 'vx': 0.0,
            'vy': 0.0, 'vz': 0.0, 'int1': -1.0, 'int2': 0.0, 'int3': 0.0,
            'ref': -1.0, 'refName': None}
    base.update(props)
    return M.AmfObj('Data.Pt', base)


class FakeMod(object):
    """够 resolve_pt 用的最小 ModView 替身。"""

    def __init__(self, templates):
        self.gdat = type('G', (), {'pts': templates, 'attacks': []})()
        self.pt_by_name = {}
        for i, t in enumerate(templates):
            nm = t.props.get('name')
            if nm:
                self.pt_by_name[nm] = (i, t)


class ResolvePtTests(unittest.TestCase):
    def test_direct_cre_with_negative_ref_keeps_its_own_int1(self):
        mod = FakeMod([])
        res = G.resolve_pt(mod, mkpt(int1=5.0, ref=-1.0), 'cre')
        self.assertEqual(res.status, 'direct')
        self.assertEqual(res.final_int1, 5)
        self.assertEqual(res.warnings, [])

    def test_template_overrides_int1_when_ref_is_in_range(self):
        tpl = mkpt(int1=78.0, vx=12.0, name='cre_sawsground')
        mod = FakeMod([mkpt(), tpl])
        res = G.resolve_pt(mod, mkpt(int1=38.0, ref=1.0), 'cre')
        self.assertEqual(res.status, 'template')
        # 这就是 saws 两个技能互换的机制：38 被模板的 78 顶掉。
        self.assertEqual(res.final_int1, 78)
        self.assertEqual(G.ival(res.final.props['vx']), 12)
        # 原始值必须留着，报告要能说明发生了什么。
        self.assertEqual(G.ival(res.original.props['int1']), 38)

    def test_ref_out_of_range_keeps_source_int1_and_warns(self):
        """HF-EX 的 globalDat 有 0 个 Pt，所有 cre.ref 都越界。"""
        mod = FakeMod([])
        res = G.resolve_pt(mod, mkpt(int1=38.0, ref=142.0), 'cre')
        self.assertEqual(res.status, 'invalid-ref')
        self.assertEqual(res.final_int1, 38)
        kinds = [w['kind'] for w in res.warnings]
        self.assertIn('pt-ref-out-of-range', kinds)

    def test_refname_resolves_ref_then_template_applies(self):
        tpl = mkpt(int1=99.0, name='cre_named')
        mod = FakeMod([mkpt(), mkpt(), tpl])
        res = G.resolve_pt(mod, mkpt(int1=1.0, ref=0.0, refName='cre_named'), 'cre')
        self.assertEqual(res.template_index, 2)
        self.assertEqual(res.final_int1, 99)

    def test_missing_refname_warns_and_falls_back_to_raw_ref(self):
        tpl = mkpt(int1=7.0)
        mod = FakeMod([tpl])
        res = G.resolve_pt(mod, mkpt(int1=3.0, ref=0.0, refName='nope'), 'cre')
        kinds = [w['kind'] for w in res.warnings]
        self.assertIn('pt-ref-name-missing', kinds)
        # ptHash 查不到时运行时不改 ref，于是 ref=0 的模板照样生效。
        self.assertEqual(res.final_int1, 7)

    def test_refname_with_ref_minus_one_is_not_resolved(self):
        """CheckCopyRefPt 的名字分支要求 ref != -1。"""
        tpl = mkpt(int1=99.0, name='cre_named')
        mod = FakeMod([tpl])
        res = G.resolve_pt(mod, mkpt(int1=4.0, ref=-1.0, refName='cre_named'), 'cre')
        self.assertEqual(res.status, 'direct')
        self.assertEqual(res.final_int1, 4)

    def test_copy_keys_match_Pt_CopyRefAttributes(self):
        self.assertEqual(
            G.PT_COPY_KEYS,
            ('facing', 'vx', 'vy', 'vz', 'int1', 'int2', 'int3'),
        )


class ItemClosureTests(unittest.TestCase):
    def _mod_with_item(self, frames, groups, actions):
        item = M.AmfObj('Data.Spt', {
            'id': 'item', 'actionGroup': G.new_ecma(groups),
            'action': G.new_ecma(actions), 'frame': G.new_ecma(frames),
            'spriteLimb': G.new_ecma([]),
        })
        mod = FakeMod([mkpt(int1=78.0, name='tpl')])
        mod.item = item
        return mod

    def test_group_is_collected_from_the_post_template_int1(self):
        """旧闭包只看 ref<0 的 cre，于是 HF-EX 一个组都没收进来。"""
        frame = M.AmfObj('Data.Frame', {
            'index': 0.0, 'cre': G.new_ecma([mkpt(int1=38.0, ref=0.0)]),
            'sfx': -1.0,
        })
        action = M.AmfObj('Data.Action', {'index': 0.0, 'frameIndex': 0.0})
        group = M.AmfObj('Data.ActionGroup', {
            'index': 0.0, 'name': 'g0',
            'actionIndex': G.new_ecma([G.new_ecma([0.0])]),
        })
        mod = self._mod_with_item([frame], [group], [action])
        clo = G.ItemClosure(mod).expand({0})
        # 模板把 38 改写成 78，收进来的必须是 78。
        self.assertIn(78, clo.groups | {r.final_int1 for r in clo.pt_resolutions})
        self.assertEqual([r.final_int1 for r in clo.pt_resolutions], [78])


class RewriteCodeTests(unittest.TestCase):
    def _body(self, code):
        return A.MethodBody(0, 4, 2, 4, 5, code, [], [])

    def test_identity_rewrite_is_byte_exact(self):
        code = bytes([0xD0, 0x30, 0x26, 0x12, 0x02, 0x00, 0x00, 0x02, 0x47])
        body = self._body(code)
        A.rewrite_code(body)
        self.assertEqual(body.code, code)

    def test_insertion_relocates_a_forward_branch(self):
        # getlocal0; pushscope; pushtrue; iffalse +2 (跳过两条 nop); nop; nop; returnvoid
        # s24 是小端：+2 编码成 02 00 00。
        code = bytes([0xD0, 0x30, 0x26, 0x12, 0x02, 0x00, 0x00, 0x02, 0x02, 0x47])
        body = self._body(code)
        target_before = None
        for off, op, ops, nxt in A.walk_code(code):
            if op == 0x12:
                target_before = nxt + ops[0]
        self.assertEqual(target_before, 9)          # 就是那条 returnvoid
        A.rewrite_code(body, before={2: [(0x02, []), (0x02, [])]})
        target_after = None
        for off, op, ops, nxt in A.walk_code(body.code):
            if op == 0x12:
                target_after = nxt + ops[0]
        self.assertEqual(len(body.code), len(code) + 2)
        # 跳转仍然落在那条 returnvoid 上，而不是原来的数字偏移。
        self.assertEqual(target_after, len(body.code) - 1)
        self.assertEqual(target_after - target_before, 2)

    def test_empty_replacement_deletes_and_keeps_targets_valid(self):
        code = bytes([0xD0, 0x30, 0x26, 0x12, 0x02, 0x00, 0x00, 0x02, 0x02, 0x47])
        body = self._body(code)
        A.rewrite_code(body, replace={7: []})
        ops = [op for _, op, _, _ in A.walk_code(body.code)]
        self.assertEqual(ops, [0xD0, 0x30, 0x26, 0x12, 0x02, 0x47])
        self.assertEqual(len(body.code), len(code) - 1)
        for off, op, args, nxt in A.walk_code(body.code):
            if op == 0x12:
                # 删掉一条 nop 后，跳转必须仍然指向 returnvoid（最后一个字节）。
                self.assertEqual(nxt + args[0], len(body.code) - 1)

    def test_branch_instructions_may_not_be_injected(self):
        body = self._body(bytes([0xD0, 0x30, 0x47]))
        with self.assertRaises(ValueError):
            A.rewrite_code(body, before={2: [(0x10, [0])]})

    def test_edits_must_land_on_instruction_boundaries(self):
        body = self._body(bytes([0xD0, 0x30, 0x2C, 0x01, 0x47]))
        with self.assertRaises(ValueError):
            A.rewrite_code(body, before={3: [(0x02, [])]})


class MultinameNameTests(unittest.TestCase):
    """mn_name 必须区分 QName 和命名空间集 Multiname 的名字位置。

    把两者混起来会让 ObjH::Run 里的 `getproperty int1`（ns-set Multiname）被读成
    别的字段名，从而得出"这个方法根本不读 int1"的错误结论。
    """

    def _abc(self):
        abc = A.AbcFile()
        abc.strings = [None, 'int1', 'Watcher', '']
        abc.namespaces = [None, (0x16, 3)]
        abc.ns_sets = [None, (1,)]
        abc.multinames = [
            None,
            (A.MN_QName, (1, 1)),        # QName: ns=1, name='int1'
            (A.MN_Multiname, (1, 1)),    # Multiname: name='int1', ns_set=1
            (A.MN_MultinameL, (1,)),
        ]
        return abc

    def test_qname_and_nsset_multiname_both_resolve_to_the_same_name(self):
        abc = self._abc()
        self.assertEqual(abc.mn_name(1), 'int1')
        self.assertEqual(abc.mn_name(2), 'int1')

    def test_multinamel_and_zero_have_no_name(self):
        abc = self._abc()
        self.assertIsNone(abc.mn_name(3))
        self.assertIsNone(abc.mn_name(0))


class DeferredRoutingPrimitiveTests(unittest.TestCase):
    """给"独立 itemSpt + cre 路由"那一层准备的原语。

    **本轮构建不使用它们**（用户选的是只拆 LMI、不动字节码）。这里只验证编码层面
    正确、ABC 仍能往返 —— 不代表那一层的运行时行为已经验证过。留着它们是因为
    计划里写明了后续可能加路由；不加测试就会变成"看起来已经就绪"的死代码。
    """

    def _abc(self):
        raw = A.build_asset_abc([('Data', 'Global_probeLmi', 'mx.core', 'ByteArrayAsset')])
        abc = A.AbcFile.parse(raw)
        self.assertEqual(abc.build(), raw, '基线资产 ABC 本身就不往返')
        return abc

    def test_added_instance_slot_keeps_the_abc_round_tripping(self):
        abc = self._abc()
        qname = abc.add_instance_slot('Data', 'Global_probeLmi', 'itemSptRoute')
        rebuilt = abc.build()
        again = A.AbcFile.parse(rebuilt)
        self.assertEqual(again.build(), rebuilt)
        trait = again.find_class_trait('Data', 'Global_probeLmi', 'itemSptRoute',
                                       static=False, kinds=(0,))
        self.assertIsNotNone(trait)
        # 类型索引 0 = `*`：给新槽位标一个解析不到的多名会让 AVM2 verifier 拒类。
        self.assertEqual(trait.data[1], 0)
        self.assertEqual(abc.mn_name(qname), 'itemSptRoute')

    def test_slot_id_follows_the_class_convention(self):
        """全是 0（交给 AVM2 自动分配）的类，新槽位也必须写 0。

        HF 的 ABC 里每个 slot trait 的 slot_id 都是 0。写 max+1=1 会和第一个自动
        分配到的槽位撞号，整个类被 verifier 拒掉 —— 静默退出，结构检查全绿。
        """
        from hfport.abcpatch import Trait
        abc = self._abc()
        ci = abc.find_instance('Data', 'Global_probeLmi')
        slots = [t for t in abc.instances[ci]['traits'] if (t.kind & 0x0F) in (0, 6)]
        self.assertTrue(all(t.data[0] == 0 for t in slots) or not slots)
        abc.add_instance_slot('Data', 'Global_probeLmi', 'zeroConvention')
        added = abc.find_class_trait('Data', 'Global_probeLmi', 'zeroConvention',
                                     static=False, kinds=(0,))
        self.assertEqual(added.data[0], 0)

        # 反面：类本来就用显式编号时，才接着往上编号。
        abc2 = self._abc()
        ci2 = abc2.find_instance('Data', 'Global_probeLmi')
        abc2.instances[ci2]['traits'].append(
            Trait(abc2.qname_idx(0x16, '', 'explicitSlot'), 0x00, (7, 0, 0), []))
        abc2.add_instance_slot('Data', 'Global_probeLmi', 'nextOne')
        nxt = abc2.find_class_trait('Data', 'Global_probeLmi', 'nextOne',
                                    static=False, kinds=(0,))
        self.assertEqual(nxt.data[0], 8)

    def test_adding_the_same_slot_twice_is_refused(self):
        abc = self._abc()
        abc.add_instance_slot('Data', 'Global_probeLmi', 'itemSptRoute')
        with self.assertRaises(ValueError):
            abc.add_instance_slot('Data', 'Global_probeLmi', 'itemSptRoute')

    def test_added_method_round_trips_with_explicit_scope_depth(self):
        abc = self._abc()
        code = bytes([0xD0, 0x30, 0x47])          # getlocal0; pushscope; returnvoid
        _, trait, body = abc.add_class_method(
            'Data', 'Global_probeLmi', 'hfpProbe', [], code=code, max_stack=2,
            init_scope_depth=4, max_scope_depth=5)
        self.assertEqual((body.init_scope_depth, body.max_scope_depth), (4, 5))
        rebuilt = abc.build()
        again = A.AbcFile.parse(rebuilt)
        self.assertEqual(again.build(), rebuilt)
        self.assertIsNotNone(again.find_class_trait(
            'Data', 'Global_probeLmi', 'hfpProbe', static=False, kinds=(1,)))

    def test_scope_depth_is_refused_rather_than_guessed(self):
        """没有同类方法可参照时必须报错，不能默默填一个深度。

        这些 Flex 嵌套类不是从深度 1 进入的（实测：Data.Global 的静态方法是 3，
        Game.World 的实例方法是 4，Game.ObjH 是 5），填错会让整个类 verify 失败，
        表现是启动 4 秒静默退出、而且所有结构检查都是绿的。生成的资产类只有 iinit
        而没有方法 trait，所以这里正好是"无参照"的情形。
        """
        abc = self._abc()
        self.assertIsNone(
            abc.dominant_scope_depths('Data', 'Global_probeLmi', static=False))
        self.assertIsNone(abc.dominant_scope_depths('Data', 'NoSuchClass'))
        with self.assertRaises(RuntimeError):
            abc.add_class_method('Data', 'Global_probeLmi', 'hfpGuess', [],
                                 code=bytes([0xD0, 0x30, 0x47]), max_stack=2)


class ContainerSelfcheckTests(unittest.TestCase):
    """容器自检必须能区分"只是压缩形式不同"和"标签真的丢了"。

    用真的 SwfFile 造样本：自检里会把重建结果再解析一遍，喂假字节根本走不到判定。
    """

    def _swf(self, tags):
        from hfport import swf as S
        # header_rest = 一个 1 字节的 rect(nbits=0) + framerate + framecount
        return S.SwfFile(b'FWS', 10, b'\x00\x00\x00\x01\x00', list(tags))

    def _tags(self):
        from hfport import swf as S
        return [S.Tag(S.TAG_DEFINEBINARYDATA, b'\x01\x00\x00\x00\x00\x00abc'),
                S.Tag(S.TAG_END, b'')]

    def _builder(self, swf):
        from hfport.builder import Builder
        b = Builder.__new__(Builder)
        b.dst = type('V', (), {'swf': swf})()
        return b

    def test_identical_bytes_report_identical(self):
        swf = self._swf(self._tags())
        raw = swf.build()
        got = self._builder(swf)._selfcheck_container(raw, lambda *a: None)
        self.assertEqual(got['result'], 'identical')
        self.assertEqual(got['orig_bytes'], got['rebuilt_bytes'])

    def test_compression_only_is_tolerated_and_labelled(self):
        import struct
        import zlib
        from hfport import swf as S
        swf = self._swf(self._tags())
        plain = swf.build()
        # 造一个真 CWS：同样的内容，压缩容器。字节不同、标签一致。
        raw = b'CWS' + plain[3:8] + zlib.compress(plain[8:])
        self.assertNotEqual(raw, plain)
        self.assertEqual(len(S.SwfFile.parse(raw).tags), len(swf.tags))
        got = self._builder(swf)._selfcheck_container(raw, lambda *a: None)
        self.assertEqual(got['result'], 'compression-only')
        self.assertTrue(got['was_compressed'])
        self.assertEqual(got['orig_signature'], 'CWS')
        self.assertEqual(got['tags'], len(swf.tags))

    def test_content_difference_is_a_hard_failure(self):
        """规范化之后还不一致 = 解析/重发射真的丢了东西，必须拒绝构建。

        这一支之前写成"重建结果再解析一遍和自己比"，按构造必然相等，是个不可能
        失败的检查；现在比的是传进来的原始字节，所以它真的能红。
        """
        from hfport import swf as S
        swf = self._swf(self._tags())
        richer = self._swf(list(self._tags())[:1]
                           + [S.Tag(S.TAG_DEFINEBINARYDATA,
                                    b'\x02\x00\x00\x00\x00\x00zz')]
                           + [S.Tag(S.TAG_END, b'')])
        raw = richer.build()
        self.assertNotEqual(raw, swf.build())
        with self.assertRaises(RuntimeError) as ctx:
            self._builder(swf)._selfcheck_container(raw, lambda *a: None)
        self.assertIn('往返丢信息', str(ctx.exception))

    def test_compression_only_requires_identical_content(self):
        """压缩容器里内容不同的话，不能被当成"只是压缩形式不同"放过。"""
        import zlib
        from hfport import swf as S
        swf = self._swf(self._tags())
        richer = self._swf(list(self._tags())[:1]
                           + [S.Tag(S.TAG_DEFINEBINARYDATA,
                                    b'\x03\x00\x00\x00\x00\x00qq')]
                           + [S.Tag(S.TAG_END, b'')])
        plain = richer.build()
        raw = b'CWS' + plain[3:8] + zlib.compress(plain[8:])
        with self.assertRaises(RuntimeError):
            self._builder(swf)._selfcheck_container(raw, lambda *a: None)


if __name__ == '__main__':
    unittest.main()
