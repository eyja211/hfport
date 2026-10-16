# -*- coding: utf-8 -*-
"""新增贴图必须在原生 SPT 之前注册；SPT 本身仍按 refSpt 顺序晚加载。"""
from . import abcpatch as A


HOOK_A = ('Data', 'Global', 'LoadGlobalDatFromCompressedBytes')
HOOK_B = ('Data', 'BgInfoFile', 'LoadFromCompressedBytes')


def split_load_sequence(sequence):
    """稳定分组，不改变 LMI 内部顺序或 SPT 的 refSpt 拓扑序。"""
    lmis, spts = [], []
    classes, paths = set(), set()
    for kind, cls, path in sequence:
        if kind not in ('lmi', 'spt'):
            raise ValueError('未知资源加载类型: %r' % kind)
        if cls in classes or path in paths:
            raise ValueError('重复的资源加载: %s / %s' % (cls, path))
        classes.add(cls)
        paths.add(path)
        (lmis if kind == 'lmi' else spts).append((kind, cls, path))
    return lmis, spts


def emit_loads(asm, sequence):
    abc = asm.abc
    for kind, cls, path in sequence:
        loader = {'lmi': 'LimbInfoFile', 'spt': 'Spt'}[kind]
        package, short = cls.rsplit('.', 1)
        asm.getlex(abc.qname_idx(0x16, 'Data', loader))
        asm.getlex(abc.qname_idx(0x16, package, short))
        asm.construct(0)
        asm.getlex(abc.qname_idx(0x16, 'flash.utils', 'ByteArray'))
        asm._op(0x87, stack_delta=-1)  # astypelate
        asm.pushstring(path)
        asm.callpropvoid(abc.qname_idx(0x16, '', 'LoadFromCompressedBytes'), 2)


def append_load_hooks(abc, sequence, log=print):
    lmis, spts = split_load_sequence(sequence)
    for label, spec, entries in (('A 贴图预加载', HOOK_A, lmis),
                                  ('B SPT 加载', HOOK_B, spts)):
        if not entries:
            continue
        mi, _ = abc.find_class_method(*spec)
        body = abc.body_of_method(mi) if mi is not None else None
        if body is None:
            raise RuntimeError('资源加载宿主缺失: %s.%s::%s' % spec)
        asm = A.Asm(abc)
        emit_loads(asm, entries)
        code = asm.finish()
        A.append_tail(abc, body, code, asm.max_stack)
        log('   %s: %d 字节（%d 个加载调用）' % (label, len(code), len(entries)))


def read_load_calls(abc, body):
    """识别工具发射的完整加载表达式，返回 (kind, class, path)。

    不把常量池/孤立 pushstring 当成调用；receiver、资产类和 ByteArray 的命名空间
    都要匹配。这里只识别工具的直线表达式，不试图解释原生混淆加载器的任意表达式。
    """
    insns = list(A.walk_code(body.code))
    out = []
    for i in range(len(insns) - 6):
        block = insns[i:i + 7]
        if [x[1] for x in block] != [0x60, 0x60, 0x42, 0x60, 0x87, 0x2C, 0x4F]:
            continue
        ops = [x[2] for x in block]
        receiver = abc.mn_str(ops[0][0])
        kind = {'Data::LimbInfoFile': 'lmi', 'Data::Spt': 'spt'}.get(receiver)
        if (kind is None or ops[2] != [0]
                or abc.mn_str(ops[3][0]) != 'flash.utils::ByteArray'
                or abc.mn_str(ops[6][0]) != '::LoadFromCompressedBytes'
                or ops[6][1] != 2):
            continue
        asset = abc.mn_str(ops[1][0])
        if not asset.startswith('Data::'):
            continue
        out.append((kind, asset.replace('::', '.', 1), abc.strings[ops[5][0]]))
    return out
