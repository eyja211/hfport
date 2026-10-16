# -*- coding: utf-8 -*-
"""SPT 在读取时绑定 limb；事后填满 Global.limb 不能修复已经绑定的空值。"""
import struct
import unittest
from types import SimpleNamespace
from unittest import mock

from hfport import abcpatch as A
from hfport import amf as M
from hfport import loaders as L
from hfport import verify as V
from hfport import gamedata as G
from hfport import swf as S
from hfport.builder import Builder
from hfport.repair import CreRepair


SEQUENCE = [
    ('lmi', 'Data.Global_test_itemLmi', 'lmi_data/test_item.lmi'),
    ('spt', 'Data.Global_test_itemSpt', 'spt_data/test_item.spt'),
    ('lmi', 'Data.Global_test_roleLmi', 'lmi_data/test_role.lmi'),
    ('spt', 'Data.Global_model_testSpt', 'spt_data/model_test.spt'),
    ('spt', 'Data.Global_role_testSpt', 'spt_data/role_test.spt'),
]
HOOK_A = ('Data', 'Global', 'LoadGlobalDatFromCompressedBytes')
HOOK_B = ('Data', 'BgInfoFile', 'LoadFromCompressedBytes')


def host_abc():
    abc = A.AbcFile.parse(A.build_asset_abc([
        ('Data', 'Global', '', 'Object'),
        ('Data', 'BgInfoFile', '', 'Object'),
        ('Web_misc', 'SelectCharPanel', '', 'Object'),
    ]))
    for pkg, cls, method in (HOOK_A, HOOK_B, ('Data', 'Global', 'InitPow'),
                              ('Data', 'Global', 'LoadAllData')):
        # B 的返回值要在尾部加载过程中留在栈上。
        code = b'\xd0\x30\x20\x48' if (pkg, cls, method) == HOOK_B else b'\xd0\x30\x47'
        abc.add_class_method(pkg, cls, method, [], static=True, code=code,
                             init_scope_depth=0, max_scope_depth=1)
    ci = abc.find_instance('Web_misc', 'SelectCharPanel')
    abc.classes[ci][1].append(A.Trait(
        abc.qname_idx(0x05, 'private', 'charOption'), 0, (0, 0, 0), []))
    abc.ns_sets.append([abc.ns_idx(0x16, '')])
    abc.multinames.append((A.MN_MultinameL, (len(abc.ns_sets) - 1,)))
    return abc


def swf_of(abc):
    return S.SwfFile(b'FWS', 10, b'\x08\x00\x00\x1e\x01\x00', [
        S.Tag(S.TAG_DOABC, struct.pack('<I', 0) + b'frame2\0' + abc.build(), True)])


def body_of(abc, spec):
    mi, _ = abc.find_class_method(*spec)
    return abc.body_of_method(mi)


def patch_builder(abc, sequence):
    b = Builder('', '', '', [], '', log=lambda *a: None)
    b.dst = SimpleNamespace(swf=swf_of(abc))
    b.char_meta = [dict(new_id='role_test', pow=[], langwords=[], bind_to='lucas',
                        value=1, display='Test')]
    b.manifest = {'load_sequence': sequence, 'new_sounds': []}
    with mock.patch.object(b, '_patch_charlist_pages'):
        b._patch_abc()
    return A.AbcFile.parse(S.parse_doabc(b.dst.swf.tags[0].body)[2])


def patch_repair(abc, sequence):
    r = CreRepair('', '', '', [], 'test', '', log=lambda *a: None)
    r.dst = SimpleNamespace(swf=swf_of(abc))
    r.load_sequence = sequence
    r.new_p = {cls: b'' for _, cls, _ in sequence}
    # 路由层另有真 EXE 集成验证；这里仅隔离共用的资源加载入口。
    with mock.patch('hfport.repair.IR.apply_item_route', return_value={}):
        r._patch_abc()
    return A.AbcFile.parse(S.parse_doabc(r.dst.swf.tags[0].body)[2])


def execute_loads(abc, spec, load):
    """执行真实发射的 7 条加载指令；不把 pushstring 当成加载成功。

    宿主里的注册语句不属于此模拟器。仅截取以 Data.LimbInfoFile/Data.Spt 为
    receiver 的加载表达式，表达式内任何非预期 opcode 都报错。
    """
    ins = list(A.walk_code(body_of(abc, spec).code))
    calls = []
    for i, (_, op, args, _) in enumerate(ins):
        if op != 0x60 or abc.mn_str(args[0]) not in ('Data::LimbInfoFile', 'Data::Spt'):
            continue
        stack = []
        for _, cop, cargs, _ in ins[i:i + 7]:
            if cop == 0x60:
                stack.append(abc.mn_str(cargs[0]))
            elif cop == 0x42:
                assert cargs == [0]
                stack.append(('instance', stack.pop()))
            elif cop == 0x87:
                assert stack.pop() == 'flash.utils::ByteArray'
                assert stack[-1][0] == 'instance'
            elif cop == 0x2C:
                stack.append(abc.strings[cargs[0]])
            elif cop == 0x4F:
                assert abc.mn_str(cargs[0]) == '::LoadFromCompressedBytes' and cargs[1] == 2
                path, instance, receiver = stack.pop(), stack.pop(), stack.pop()
                kind = 'lmi' if receiver == 'Data::LimbInfoFile' else 'spt'
                cls = instance[1].replace('::', '.')
                calls.append((kind, cls, path))
                load(kind, cls, path)
            else:
                raise AssertionError('unmodelled load opcode %02x' % cop)
        assert not stack
    return calls


def calls_of(abc, spec):
    return execute_loads(abc, spec, lambda *a: None)


class TextureLoadOrderTests(unittest.TestCase):
    def test_builder_loads_all_lmis_in_a_and_only_spts_in_b(self):
        abc = patch_builder(host_abc(), SEQUENCE)
        self.assertEqual(calls_of(abc, HOOK_A), [x for x in SEQUENCE if x[0] == 'lmi'])
        self.assertEqual(calls_of(abc, HOOK_B), [x for x in SEQUENCE if x[0] == 'spt'])
        self.assertEqual(body_of(abc, HOOK_B).code[-1], 0x48)

    def test_repair_loads_item_lmi_in_a_and_item_spt_in_b(self):
        seq = SEQUENCE[:2]
        abc = patch_repair(host_abc(), seq)
        self.assertEqual(calls_of(abc, HOOK_A), [seq[0]])
        self.assertEqual(calls_of(abc, HOOK_B), [seq[1]])

    def test_stock_role_and_item_can_borrow_both_kinds_of_imported_art(self):
        abc = patch_builder(host_abc(), SEQUENCE)
        limbs, bound = {}, {}
        art = {SEQUENCE[0][2]: ('imported_item@ns', object()),
               SEQUENCE[2][2]: ('imported_role@ns', object())}
        names = [v[0] for v in art.values()]

        def spt(path):
            # Spt.LoadFromBytes: 主 limbName 与六个 limbNames 换色分支在此刻绑定。
            bound[path] = [(limbs.get(n), {c: limbs.get(n) for c in 'abcdef'}) for n in names]

        def load(kind, cls, path):
            if kind == 'lmi':
                name, pic = art[path]
                limbs[name] = pic
            else:
                spt(path)

        execute_loads(abc, HOOK_A, load)
        spt('stock_role')
        spt('stock_item')
        execute_loads(abc, HOOK_B, load)
        for consumer in ('stock_role', 'stock_item', SEQUENCE[-1][2], SEQUENCE[1][2]):
            for i, (_, expected) in enumerate(art.values()):
                with self.subTest(consumer=consumer, limb=names[i]):
                    main, colors = bound[consumer][i]
                    self.assertIs(main, expected, '晚加载不能补救 SPT 早先绑定的空 limb')
                    for color in 'abcdef':
                        self.assertIs(colors[color], expected)

    def test_no_item_pack_and_ref_spt_order(self):
        sequence = SEQUENCE[2:]
        abc = patch_builder(host_abc(), sequence)
        self.assertEqual(calls_of(abc, HOOK_A), [sequence[0]])
        self.assertEqual(calls_of(abc, HOOK_B), sequence[1:])
        self.assertEqual(V._check_load_hooks(swf_of(abc), {'load_sequence': sequence}), [])

    def test_consecutive_ports_preserve_both_batches(self):
        first = patch_builder(host_abc(), SEQUENCE)
        second_seq = [(k, c.replace('test', 'next'), p.replace('test', 'next'))
                      for k, c, p in SEQUENCE]
        second = patch_builder(first, second_seq)
        for kind, spec in (('lmi', HOOK_A), ('spt', HOOK_B)):
            self.assertEqual(calls_of(second, spec),
                             [x for x in SEQUENCE + second_seq if x[0] == kind])
        for seq in (SEQUENCE, second_seq):
            self.assertEqual(V._check_load_hooks(swf_of(second), {'load_sequence': seq}), [])

    def test_added_load_code_is_straight_line_and_stack_neutral(self):
        abc = host_abc()
        for kind in ('lmi', 'spt'):
            asm = A.Asm(abc)
            L.emit_loads(asm, [x for x in SEQUENCE if x[0] == kind])
            code = asm.finish()
            self.assertEqual(asm.stack, 0)
            self.assertEqual(asm.max_stack, 3)
            self.assertFalse(any(A.OPCODES[op][1] == ['s24']
                                 for _, op, _, _ in A.walk_code(code)))

    def test_old_late_loading_reproduces_air_person_not_new_role_failure(self):
        abc = host_abc()
        # 旧构建器的策略：整个清单在 B 加载。用同一发射器重现，仅改变阶段。
        append_calls(abc, HOOK_B, SEQUENCE)
        limbs, bindings = {}, {}
        def load(kind, cls, path):
            if kind == 'lmi':
                limbs[path] = object()
            else:
                bindings[path] = limbs.get(SEQUENCE[2][2])
        execute_loads(abc, HOOK_A, load)
        bindings['stock_role'] = limbs.get(SEQUENCE[2][2])
        bindings['stock_item'] = limbs.get(SEQUENCE[2][2])
        execute_loads(abc, HOOK_B, load)
        self.assertIsNone(bindings['stock_role'])
        self.assertIsNone(bindings['stock_item'])
        self.assertIsNotNone(bindings[SEQUENCE[-1][2]])
        self.assertTrue(V._check_load_hooks(swf_of(abc), {'load_sequence': SEQUENCE}))

    def test_verifier_checks_loader_asset_and_stage_not_just_paths(self):
        for defect in ('missing', 'path_only', 'wrong_asset', 'wrong_receiver',
                       'duplicate', 'late_lmi', 'early_spt', 'reverse_spts'):
            with self.subTest(defect=defect):
                abc = host_abc()
                lmis, spts = L.split_load_sequence(SEQUENCE)
                a, b = list(lmis), list(spts)
                if defect == 'missing':
                    a.pop()
                elif defect == 'path_only':
                    _, _, path = a.pop()
                    asm = A.Asm(abc)
                    asm.pushstring(path)
                    asm._op(0x29, stack_delta=-1)  # pop
                    A.append_tail(abc, body_of(abc, HOOK_A), asm.finish(), asm.max_stack)
                elif defect == 'wrong_asset':
                    k, c, p = a[-1]
                    a[-1] = (k, 'Data.Global_wrongLmi', p)
                elif defect == 'wrong_receiver':
                    k, c, p = a[-1]
                    a[-1] = ('spt', c, p)
                elif defect == 'duplicate':
                    a.append(a[0])
                elif defect == 'late_lmi':
                    b.insert(0, a.pop())
                elif defect == 'early_spt':
                    a.append(b.pop())
                else:
                    b.reverse()
                append_calls(abc, HOOK_A, a)
                append_calls(abc, HOOK_B, b)
                self.assertTrue(V._check_load_hooks(swf_of(abc), {'load_sequence': SEQUENCE}))

    def test_verifier_accepts_both_tool_entrypoints(self):
        for patch, sequence in ((patch_builder, SEQUENCE), (patch_repair, SEQUENCE[:2])):
            abc = patch(host_abc(), sequence)
            self.assertEqual(V._check_load_hooks(swf_of(abc), {'load_sequence': sequence}), [])

    def test_duplicate_manifest_is_rejected_before_patching(self):
        with self.assertRaisesRegex(ValueError, '重复'):
            L.split_load_sequence(SEQUENCE + SEQUENCE[:1])
        with self.assertRaisesRegex(ValueError, '未知'):
            L.split_load_sequence([('other', 'Data.Global_bad', 'bad')])

    def test_modview_uses_early_native_late_order_for_png_winners(self):
        abc = host_abc()
        early = ('lmi', 'Data.Global_zearlyLmi', 'lmi_data/zearly.lmi')
        native = ('lmi', 'Data.Global_ynativeLmi', 'lmi_data/ynative.lmi')
        late = ('lmi', 'Data.Global_xlateLmi', 'lmi_data/xlate.lmi')
        for spec, entry in ((HOOK_A, early), (('Data', 'Global', 'LoadAllData'), native),
                             (HOOK_B, late)):
            append_calls(abc, spec, [entry])
        view = object.__new__(G.ModView)
        view.swf = swf_of(abc)
        syms = bytearray(struct.pack('<H', 3))
        for idx, entry in enumerate((early, native, late), 1):
            pic = M.AmfObj('Data.LimbPic')
            pic.props.update(filename='shared.png', embeded=True)
            lmi = G.LmiFile([pic], [entry[2].encode()], [])
            view.swf.tags.append(S.Tag(S.TAG_DEFINEBINARYDATA,
                struct.pack('<HI', idx, 0) + M.wrap_payload(lmi.build()), True))
            syms += struct.pack('<H', idx) + entry[1].encode() + b'\0'
        view.swf.tags.append(S.Tag(S.TAG_SYMBOLCLASS, bytes(syms)))
        self.assertEqual(view._declared_lmi_load_order(), [early[2], native[2], late[2]])
        view.load_all_lmis()
        self.assertEqual(view.pngs['shared.png'][0], late[2])
        self.assertEqual(view.pngs['shared.png'][1], late[2].encode())


def append_calls(abc, spec, sequence):
    asm = A.Asm(abc)
    L.emit_loads(asm, sequence)
    A.append_tail(abc, body_of(abc, spec), asm.finish(), asm.max_stack)


if __name__ == '__main__':
    unittest.main()
