# -*- coding: utf-8 -*-
"""M3/M4 测试：完整构建单角色试点 EXE，并做 FFDec 冒烟 + 补丁反编译抽查。"""
import sys, os, subprocess
sys.stdout.reconfigure(encoding='utf-8')
BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, BASE)

from hfport.builder import Builder


def main():
    chars = sys.argv[1:] or ['lucas']
    out = os.path.join(BASE, 'output', 'HF Eyja 0.1.7 +PM试点.exe' if len(chars) == 1
                       else 'HF Eyja 0.1.7 +PM%d.exe' % len(chars))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    b = Builder(
        src_exe=os.path.join(BASE, 'Promised Mystery of Moisture V-1.9.6.exe'),
        dst_exe=os.path.join(BASE, 'HF Eyja 0.1.7.exe'),
        out_exe=out,
        chars=chars,
        work_dir=os.path.join(BASE, 'work'),
        ffdec_jar=os.path.join(BASE, 'FFDec', 'ffdec.jar'),
    )
    report = b.run()

    print('== FFDec 冒烟 ==')
    # FFDec 需读 .swf（非 projector EXE）：抽出内嵌 SWF
    from hfport import swf as S
    _, out_swf = S.read_projector(out)
    smoke_swf = os.path.join(BASE, 'work', 'smoke_input.swf')
    open(smoke_swf, 'wb').write(S.SwfFile.parse(out_swf).build())
    outdir = os.path.join(BASE, 'work', 'smoke_scripts')
    import shutil
    shutil.rmtree(outdir, ignore_errors=True)
    r = subprocess.run(['java', '-Xmx6g', '-jar', os.path.join(BASE, 'FFDec', 'ffdec.jar'),
                        '-selectclass', 'Data.Global,Data.BgInfoFile,Web_misc.SelectCharPanel',
                        '-export', 'script', outdir, smoke_swf],
                       capture_output=True, text=True, timeout=1800)
    print('ffdec rc=%d' % r.returncode)
    ok = True
    g = os.path.join(outdir, 'scripts', 'Data', 'Global.as')
    text = open(g, encoding='utf-8').read()
    for meta in b.char_meta:
        for token in ('pow["%s"]' % meta['new_id'],):
            if token not in text:
                print('  X Global.as 缺少', token)
                ok = False
    bg = open(os.path.join(outdir, 'scripts', 'Data', 'BgInfoFile.as'), encoding='utf-8').read()
    for kind, cls, path in report['manifest']['load_sequence']:
        if path not in bg:
            print('  X BgInfoFile.as 缺少加载', path)
            ok = False
    sel = open(os.path.join(outdir, 'scripts', 'Web_misc', 'SelectCharPanel.as'), encoding='utf-8').read()
    for meta in b.char_meta:
        if '"%s"' % meta['new_id'] not in sel:
            print('  X SelectCharPanel 缺少', meta['new_id'])
            ok = False

    # 角色一览翻页扩容校验（仅当确实扩了页才验证；反编译才是可靠口径）
    import math
    total = len(b.base_charlist) + len(b.char_meta)
    exp_pages = max(1, math.ceil(total / b.CHARLIST_PER_PAGE))
    old_pages = max(1, math.ceil(len(b.base_charlist) / b.CHARLIST_PER_PAGE))
    if exp_pages > old_pages:
        import subprocess as _sp, shutil as _sh
        from hfport import swf as _S
        _, ob = _S.read_projector(out)
        cl_swf = os.path.join(BASE, 'work', 'charlist_verify.swf')
        open(cl_swf, 'wb').write(_S.SwfFile.parse(ob).build())
        cld = os.path.join(BASE, 'work', 'charlist_verify')
        _sh.rmtree(cld, ignore_errors=True)
        _sp.run(['java', '-Xmx6g', '-jar', os.path.join(BASE, 'FFDec', 'ffdec.jar'),
                 '-selectclass', 'Page.CharList', '-export', 'script', cld, cl_swf],
                capture_output=True, text=True, timeout=900)
        cl = open(os.path.join(cld, 'scripts', 'Page', 'CharList.as'), encoding='utf-8').read()
        if 'this.pageNum < %d' % exp_pages not in cl:
            print('  X NextPage 未扩到 < %d' % exp_pages)
            ok = False
        if '"/%d"' % exp_pages not in cl and '/%d' % exp_pages not in cl:
            print('  X 页码分母未改为 /%d' % exp_pages)
            ok = False
        print('  翻页扩容: %d 人 -> %d 页（原 %d）✓' % (total, exp_pages, old_pages))
    else:
        print('  翻页: %d 人，仍 %d 页，无需扩容' % (total, exp_pages))
    print('冒烟结果:', 'OK ✓' if ok else 'FAIL')
    return ok


if __name__ == '__main__':
    sys.exit(0 if main() else 1)
