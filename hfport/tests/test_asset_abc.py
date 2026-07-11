# -*- coding: utf-8 -*-
"""M1 出口测试：生成资产类 DoABC 注入 Eyja SWF，FFDec 必须能解析并反编译新类。"""
import sys, os, subprocess
BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, BASE)

from hfport import swf as S
from hfport import abcpatch as A


def main():
    _, swf_bytes = S.read_projector(os.path.join(BASE, 'HF Eyja 0.1.7.exe'))
    f = S.SwfFile.parse(swf_bytes)

    # 主 SymbolClass = 条目最多的那个
    scs = f.symbol_classes()
    main_sc_idx, main_sc = max(scs, key=lambda x: len(x[1]))
    print('main SymbolClass tag #%d entries %d' % (main_sc_idx, len(main_sc)))

    # 生成两个测试类，绑定 lucas 的数据副本
    abc_bytes = A.build_asset_abc([
        ('Data', 'Global_zztestSpt', 'mx.core', 'ByteArrayAsset'),
        ('Data', 'Global_zztestLmi', 'mx.core', 'ByteArrayAsset'),
        ('', 'Zztest_mp3', 'mx.core', 'SoundAsset'),
    ])
    print('generated asset abc: %d bytes' % len(abc_bytes))
    # 独立解析一遍自检
    chk = A.AbcFile.parse(abc_bytes)
    assert chk.build() == abc_bytes
    print('self parse/rebuild OK, scripts=%d instances=%d' % (len(chk.scripts), len(chk.instances)))

    nid = f.max_char_id() + 1
    print('new char ids: %d %d' % (nid, nid + 1))
    pay = f.binary_payload('Data.Global_lucasSpt')
    pay2 = f.binary_payload('Data.Global_lucasLmi')

    new_tags = [S.make_doabc_tag(abc_bytes, 'hfport_test'),
                f.add_binary(nid, pay), f.add_binary(nid + 1, pay2)]
    f.insert_tags(main_sc_idx, new_tags)
    # SymbolClass 标签索引平移
    f.add_symbolclass_entries(main_sc_idx + len(new_tags), [
        (nid, 'Data.Global_zztestSpt'), (nid + 1, 'Data.Global_zztestLmi')])

    out_swf = os.path.join(BASE, 'work', 'probe_m1.swf')
    open(out_swf, 'wb').write(f.build())
    print('probe written:', out_swf, os.path.getsize(out_swf), 'bytes')

    # FFDec 冒烟：导出新类脚本
    outdir = os.path.join(BASE, 'work', 'probe_m1_scripts')
    r = subprocess.run(['java', '-Xmx4g', '-jar', os.path.join(BASE, 'FFDec', 'ffdec.jar'),
                        '-selectclass', 'Data.Global_zztestSpt,Data.Global_zztestLmi,Zztest_mp3',
                        '-export', 'script', outdir, out_swf],
                       capture_output=True, text=True, timeout=600)
    print('ffdec rc=%d' % r.returncode)
    tail = (r.stdout + r.stderr).strip().splitlines()
    print('\n'.join(tail[-6:]))
    for root, _, files in os.walk(outdir):
        for fn in files:
            p = os.path.join(root, fn)
            print('---', p)
            print(open(p, encoding='utf-8').read())


if __name__ == '__main__':
    main()
