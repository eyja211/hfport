# -*- coding: utf-8 -*-
"""命令行向导：交互式选择源/目标 EXE 与角色，执行移植并验证。

用法:
  python -m hfport.cli                      # 全交互向导
  python -m hfport.cli --config port.json   # 用配置文件（可复用）
  python -m hfport.cli --list SRC.exe       # 只列出源里可选角色
"""
import argparse
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

from . import registry as R
from . import swf as S
from . import porter as P
from .builder import Builder
from . import verify as V

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def find_ffdec():
    for cand in [os.path.join(ROOT, 'FFDec', 'ffdec.jar'),
                 os.path.join(ROOT, 'ffdec', 'ffdec.jar')]:
        if os.path.exists(cand):
            return cand
    return None


def list_selectable(exe, ffdec, work):
    """返回源可选角色 [(id, display_en)]，从 SelectCharPanel.charOption 读取（排除 random）。"""
    inv = R.load_inventory(work, ffdec, exe)
    out = []
    seen = set()
    for value, name, cid in inv.char_options:
        if cid == 'random':
            continue
        if cid in seen:
            continue
        seen.add(cid)
        disp = dict(inv.langwords.get(cid, [])).get('en', name)
        out.append((cid, disp))
    return out


def _stem(path):
    return os.path.splitext(os.path.basename(path))[0].replace(' ', '_')


def ask(prompt, default=None):
    s = input(prompt).strip()
    return s or default


def pick_exe(role, cwd):
    exes = [f for f in os.listdir(cwd) if f.lower().endswith('.exe')
            and 'workshop' not in f.lower() and 'ffdec' not in f.lower()
            and 'uninstall' not in f.lower()]
    print('\n当前目录的 EXE：')
    for i, f in enumerate(exes, 1):
        print('  %d) %s' % (i, f))
    while True:
        s = ask('选择%s版本 EXE（输序号或路径）: ' % role)
        if not s:
            continue
        if s.isdigit() and 1 <= int(s) <= len(exes):
            return os.path.join(cwd, exes[int(s) - 1])
        if os.path.exists(s):
            return s
        print('  无效，重试。')


def run_config(cfg, log=print):
    ffdec = cfg.get('ffdec') or find_ffdec()
    if not ffdec:
        raise RuntimeError('找不到 FFDec/ffdec.jar，请在配置里指定 ffdec')
    work = cfg.get('work_dir') or os.path.join(ROOT, 'work')
    os.makedirs(work, exist_ok=True)
    b = Builder(
        src_exe=cfg['src_exe'], dst_exe=cfg['dst_exe'], out_exe=cfg['out_exe'],
        chars=cfg['chars'], work_dir=work, ffdec_jar=ffdec,
        display_suffix=cfg.get('display_suffix'),
        namespace=cfg.get('namespace'), log=log)
    report = b.run()
    log('\n== 静态验证 ==')
    errs, warns = V.verify(cfg['out_exe'], report, log=log)
    if errs:
        log('\n⚠ 验证发现 %d 个问题，请勿直接游戏使用，先排查。' % len(errs))
    else:
        log('\n✓ 验证通过。请按下方清单在游戏里实测：')
        _print_checklist(report, log)
    return report, errs


def _print_checklist(report, log):
    ids = [m['new_id'] for m in report['chars']]
    log('  1) 启动不卡死（加载末尾会有数秒读条属正常）')
    log('  2) “角色一览”出现新角色: ' + '、'.join(ids[:6]) + (' 等' if len(ids) > 6 else ''))
    log('  3) VS 选人界面可选到带本次显示后缀的新角色并进战斗')
    log('  4) 抽查技能/飞行道具/贴图/音效表现与原版一致')
    log('  5) 原目标版本角色不受影响')
    log('  6) 登录后新角色等级 = 绑定角色等级（防封号）')


def wizard():
    cwd = os.getcwd()
    ffdec = find_ffdec()
    if not ffdec:
        print('❌ 未找到 FFDec/ffdec.jar'); return 1
    print('=== Hero Fighter 角色移植向导 ===')
    src = pick_exe('“源”（要移植角色的来源）', cwd)
    dst = pick_exe('“目标”（移植到哪个 mod）', cwd)
    work = os.path.join(ROOT, 'work')
    os.makedirs(work, exist_ok=True)
    print('\n读取源可选角色…')
    roster = list_selectable(src, ffdec, work)
    print('源可选角色：')
    for i, (cid, disp) in enumerate(roster, 1):
        print('  %2d) %-12s %s' % (i, cid, disp))
    sel = ask('\n要移植哪些？(逗号分隔序号，或 all): ', 'all')
    if sel.lower() == 'all':
        chars = [cid for cid, _ in roster]
    else:
        idxs = [int(x) for x in sel.replace('，', ',').split(',') if x.strip().isdigit()]
        chars = [roster[i - 1][0] for i in idxs if 1 <= i <= len(roster)]
    if not chars:
        print('未选择角色。'); return 1
    default_suffix = ' (%s)' % P.derive_namespace(src).upper()
    suffix = ask('显示名后缀 [默认 "%s"]: ' % default_suffix, default_suffix)
    default_out = os.path.join(ROOT, 'output', '%s +port%d.exe' % (_stem(dst), len(chars)))
    out = ask('输出 EXE 路径 [默认 %s]: ' % default_out, default_out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cfg = {'src_exe': src, 'dst_exe': dst, 'out_exe': out, 'chars': chars,
           'display_suffix': suffix, 'ffdec': ffdec, 'work_dir': work}
    save = os.path.join(ROOT, 'output', 'last_port_config.json')
    json.dump(cfg, open(save, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('\n配置已存: %s（下次可用 --config 复用）' % save)
    print('开始移植 %d 个角色…\n' % len(chars))
    _, errs = run_config(cfg)
    return 0 if not errs else 2


def main(argv=None):
    ap = argparse.ArgumentParser(description='Hero Fighter 角色移植工具')
    ap.add_argument('--config', help='用 JSON 配置执行（非交互）')
    ap.add_argument('--list', metavar='SRC.exe', help='列出源可选角色后退出')
    args = ap.parse_args(argv)

    if args.list:
        ffdec = find_ffdec()
        work = os.path.join(ROOT, 'work')
        os.makedirs(work, exist_ok=True)
        for cid, disp in list_selectable(args.list, ffdec, work):
            print('%-12s %s' % (cid, disp))
        return 0
    if args.config:
        cfg = json.load(open(args.config, encoding='utf-8'))
        _, errs = run_config(cfg)
        return 0 if not errs else 2
    return wizard()


if __name__ == '__main__':
    sys.exit(main())
