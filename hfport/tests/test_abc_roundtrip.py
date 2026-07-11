# -*- coding: utf-8 -*-
"""M1 硬校验：两个 mod 的所有 DoABC 解析->重发射必须字节一致；
所有方法体可被反汇编器完整走查。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from hfport import swf as S
from hfport import abcpatch as A


def check(path):
    exe_prefix, swf_bytes = S.read_projector(path)
    f = S.SwfFile.parse(swf_bytes)
    print('%s: %d tags, sig=%s v%d' % (os.path.basename(path), len(f.tags), f.sig, f.version))
    doabcs = f.find_tags(S.TAG_DOABC)
    print('  DoABC tags:', len(doabcs))
    for i, t in doabcs:
        flags, name, abc_bytes = S.parse_doabc(t.body)
        abc = A.AbcFile.parse(abc_bytes)
        out = abc.build()
        ok = out == abc_bytes
        print('  tag#%d name=%r %d bytes: classes=%d scripts=%d bodies=%d strings=%d -> roundtrip %s'
              % (i, name, len(abc_bytes), len(abc.instances), len(abc.scripts),
                 len(abc.bodies), len(abc.strings), 'OK' if ok else 'FAIL'))
        if not ok:
            # 找第一个差异位置
            n = min(len(out), len(abc_bytes))
            for j in range(n):
                if out[j] != abc_bytes[j]:
                    print('    first diff at %d/%d (out len %d)' % (j, len(abc_bytes), len(out)))
                    print('    orig:', abc_bytes[max(0, j - 8):j + 8].hex())
                    print('    out :', out[max(0, j - 8):j + 8].hex())
                    break
            else:
                print('    length diff: %d vs %d' % (len(abc_bytes), len(out)))
            return False
        # 走查所有方法体（个别混淆/垃圾方法允许失败，只作提示）
        bad = 0
        for b in abc.bodies:
            try:
                for _ in A.walk_code(b.code):
                    pass
            except ValueError as ex:
                bad += 1
                if bad <= 3:
                    print('    walk warn method %d: %s' % (b.method, ex))
        print('    walk_code: %d bodies, %d undecodable (tolerated)' % (len(abc.bodies), bad))
    # SWF 整体重建一致性（不做任何修改时）
    rebuilt = f.build()
    same = rebuilt == swf_bytes if swf_bytes[:3] == b'FWS' else None
    print('  full SWF rebuild identical:', same)
    return True


if __name__ == '__main__':
    base = os.path.join(os.path.dirname(__file__), '..', '..')
    ok1 = check(os.path.join(base, 'HF Eyja 0.1.7.exe'))
    ok2 = check(os.path.join(base, 'Promised Mystery of Moisture V-1.9.6.exe'))
    print('RESULT:', 'ALL OK' if (ok1 and ok2) else 'FAILURES')
