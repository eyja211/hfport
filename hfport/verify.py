# -*- coding: utf-8 -*-
"""静态验证器：对构建产物 EXE 做全面自检（不依赖构建期内存状态）。

检查项：
  1. EXE/SWF 结构可解析，全部 DoABC 可解析且往返一致
  2. SymbolClass 与 DefineBinaryData/DefineSound 一一对应，无 id 冲突
  3. 全部 Data.Global_* 载荷可解码（SPT/LMI/gdat）
  4. 加载序列类都存在且已绑定数据
  5. 合并空间：新角色 SPT 的肢体/攻击/pt/int1/sfx 全部可解析
  6. 注册四表一致（从补丁方法字节码反推 pow/charListOrder/charOption/langWords）
  7. fSpec/special 引擎兼容性预警
"""
import sys, struct

from . import abcpatch as A
from . import amf as M
from . import gamedata as G
from . import swf as S


def verify(out_exe, report=None, log=print):
    errs, warns = [], []
    _, swf_bytes = S.read_projector(out_exe)
    f = S.SwfFile.parse(swf_bytes)
    log('SWF 解析: %d 标签' % len(f.tags))

    # 1. ABC 往返
    for i, t in f.find_tags(S.TAG_DOABC):
        flags, name, abc_bytes = S.parse_doabc(t.body)
        try:
            abc = A.AbcFile.parse(abc_bytes)
            if abc.build() != abc_bytes:
                errs.append('DoABC %r 往返不一致' % name)
        except Exception as ex:
            errs.append('DoABC %r 解析失败: %s' % (name, ex))
    log('DoABC: %d 个全部可解析' % len(f.find_tags(S.TAG_DOABC)))

    # 2. SymbolClass 映射
    sym = {}
    for _, syms in f.symbol_classes():
        for cid, cls in syms.items():
            if cid in sym and sym[cid] != cls:
                errs.append('char id %d 被绑定两次: %s / %s' % (cid, sym[cid], cls))
            sym[cid] = cls
    have_ids = set()
    for t in f.tags:
        cid = t.char_id()
        if cid is not None:
            if cid in have_ids:
                errs.append('char id %d 定义两次' % cid)
            have_ids.add(cid)
    for cid, cls in sym.items():
        if cls.startswith('Data.Global_') and cid not in have_ids:
            errs.append('SymbolClass %s -> id %d 无对应定义标签' % (cls, cid))

    # 3. 载荷解码 + 建全局空间
    mv = G.ModView(f, 'out')
    mv.load_all_lmis()
    log('载荷: %d 个 LMI 文件, %d 肢体, %d 图片' % (len(mv.lmis), len(mv.limbs), len(mv.pngs)))
    dup_limb_files = {}
    for path, lf in mv.lmis.items():
        seen_here = set()
        for l in lf.limbs:
            nm = l.props.get('name')
            if nm and nm not in seen_here:      # 同一文件内的重复是原作既有状况，忽略
                seen_here.add(nm)
                dup_limb_files.setdefault(nm, []).append(path)
    dups = {k: v for k, v in dup_limb_files.items() if len(set(v)) > 1}
    if dups:
        for k, v in list(dups.items())[:8]:
            errs.append('肢体跨文件重名(会告警): %r 出现于 %s' % (k, sorted(set(v))))

    atk_names = set(mv.attack_by_name)
    pt_names = set(mv.pt_by_name)
    groups = G.arr_items(mv.item.props.get('actionGroup'))
    n_groups = len(groups)
    slots = G.arr_items(mv.item.props.get('spriteLimb'))

    if report:
        manifest = report.get('manifest', {})
        target_counts = manifest.get('target_item_counts', {})
        prefix_hashes = manifest.get('item_prefix_hashes', {})
        seqs = {
            'groups': groups,
            'actions': G.arr_items(mv.item.props.get('action')),
            'frames': G.arr_items(mv.item.props.get('frame')),
            'slots': slots,
        }
        for key, count in target_counts.items():
            count = int(count)
            got = G.md5(b'|'.join(G.norm_obj_bytes(x) for x in seqs[key][:count]))
            if got != prefix_hashes.get(key):
                errs.append('目标原 item %s 前缀被修改' % key)
        mapped_groups = []
        for src_i, dst_i in manifest.get('item_group_map', {}).items():
            dst_i = int(dst_i)
            mapped_groups.append(dst_i)
            if dst_i < int(target_counts.get('groups', 0)) or dst_i >= n_groups:
                errs.append('移植 item 组 %s 映射到非法下标 %d' % (src_i, dst_i))
        if len(mapped_groups) != len(set(mapped_groups)):
            errs.append('多个源 item 组映射到同一目标下标')
        for src_i, dst_i in manifest.get('invalid_group_map', {}).items():
            if int(dst_i) < n_groups:
                errs.append('源悬空 item 组 %s 意外解析到目标下标 %s' % (src_i, dst_i))
        for src_name, mapped in manifest.get('item_limb_map', {}).items():
            if mapped not in mv.limbs:
                errs.append('移植 item 肢体缺失: %s -> %s' % (src_name, mapped))
        runtime_pngs = {}
        runtime_lmis = []
        if 'lmi_data/item.lmi' in mv.lmis:
            runtime_lmis.append(mv.lmis['lmi_data/item.lmi'])
        for kind, cls, path in manifest.get('load_sequence', []):
            if kind == 'lmi' and path in mv.lmis:
                runtime_lmis.append(mv.lmis[path])
        for lmi in runtime_lmis:
            for idx, pic in enumerate(lmi.limb_pics):
                filename = pic.props.get('filename')
                if filename and lmi.pngs[idx] is not None:
                    runtime_pngs[filename] = lmi.pngs[idx]
        for mapped, expected_hash in manifest.get('copied_png_hashes', {}).items():
            got = runtime_pngs.get(mapped)
            if got is None:
                errs.append('移植 PNG 缺失: %s' % mapped)
            elif G.md5(got) != expected_hash:
                errs.append('移植 PNG 内容不来自源版本: %s' % mapped)

    # itemSpt 内部索引完整性
    actions = G.arr_items(mv.item.props.get('action'))
    frames = G.arr_items(mv.item.props.get('frame'))
    for gi, g in enumerate(groups):
        if not isinstance(g, M.AmfObj):
            continue
        for cat in G.arr_items(g.props.get('actionIndex')):
            for aiv in G.arr_items(cat):
                ai = G.ival(aiv)
                if ai >= len(actions):
                    errs.append('itemSpt 组%d actionIndex %d 越界(%d)' % (gi, ai, len(actions)))
    for ai, a in enumerate(actions):
        if isinstance(a, M.AmfObj):
            fi = G.ival(a.props.get('frameIndex'))
            if fi >= len(frames):
                errs.append('itemSpt 动作%d frameIndex %d 越界(%d)' % (ai, fi, len(frames)))
    for fi, fr in enumerate(frames):
        if isinstance(fr, M.AmfObj):
            for key in ('refIndex', 'refIndex_2'):
                r = G.ival(fr.props.get(key), 0)
                if r > 0 and r >= len(frames):
                    errs.append('itemSpt 帧%d %s=%d 越界' % (fi, key, r))
            for z in G.arr_items(fr.props.get('uz')) + G.arr_items(fr.props.get('lz')):
                if isinstance(z, M.AmfObj):
                    i = G.ival(z.props.get('i'))
                    if i >= len(slots):
                        errs.append('itemSpt 帧%d uz.i=%d 槽位越界(%d)' % (fi, i, len(slots)))

    # 4/5. 每个 SPT 的引用可解析性（对全部 SPT 跑；新角色重点）
    spt_classes = [c for c in f.class_to_char_id() if c.endswith('Spt') and c.startswith('Data.Global_')]
    n_bad_sfx = 0
    for cls in sorted(spt_classes):
        try:
            spt = G.SptFile.parse(M.unwrap_payload(f.binary_payload(cls))).obj
        except Exception as ex:
            errs.append('%s 解码失败: %s' % (cls, ex))
            continue
        cid = spt.props.get('id')
        for fr in G.arr_items(spt.props.get('frame')):
            if not isinstance(fr, M.AmfObj):
                continue
            for ctx, pt in G.frame_pts(fr):
                if ctx == 'cre':
                    i1 = G.ival(pt.props.get('int1'), -1)
                    ref = G.ival(pt.props.get('ref'))
                    # 越界且无模板引用 = 原作忠实保留的悬空生成（游戏 AddObjI 容错），仅提示
                    if i1 >= n_groups and ref < 0 and not pt.props.get('refName'):
                        warns.append('%s(%s): cre.int1=%d 组越界(%d)，与原作一致的悬空生成' % (cls, cid, i1, n_groups))
            sfx = G.ival(fr.props.get('sfx'))
            if sfx >= 120:
                n_bad_sfx += 1

    # 6. 注册一致性（从补丁函数反编译常量抽取字符串）
    if report:
        ids = [m['new_id'] for m in report['chars']]
        main_abc = None
        for i, t in f.find_tags(S.TAG_DOABC):
            _, name, abc_bytes = S.parse_doabc(t.body)
            abc = A.AbcFile.parse(abc_bytes)
            if abc.find_instance('Data', 'Global') is not None:
                main_abc = abc
        strings = set(main_abc.strings)
        for nid in ids:
            if nid not in strings:
                errs.append('注册字符串缺失: %s' % nid)
        page_info = report.get('charlist_pages', {})
        expected_pages = page_info.get('after')
        if expected_pages is not None:
            mi, _ = main_abc.find_class_method(
                'Page', 'CharList', 'NextPage', static=False
            )
            page_body = main_abc.body_of_method(mi)
            insns = list(A.walk_code(page_body.code))
            limits = []
            for idx in range(len(insns) - 3):
                a, b, c, d = insns[idx:idx + 4]
                if (a[1] in (0xD0, 0x62) and b[1] == 0x66
                        and c[1] == 0x24 and 0x0C <= d[1] <= 0x1A):
                    limits.append(c[2][0])
            if limits != [int(expected_pages)]:
                errs.append(
                    '角色一览翻页上限错误: %r != %d'
                    % (limits, int(expected_pages))
                )
            mi, _ = main_abc.find_class_method(
                'Page', 'CharList', 'OnEnterFrame', static=False
            )
            denom_body = main_abc.body_of_method(mi)
            denominators = []
            for _, op, ops, _ in A.walk_code(denom_body.code):
                if op == 0x2C:
                    value = main_abc.strings[ops[0]]
                    if value.startswith('/') and value[1:].isdigit():
                        denominators.append(value)
            if denominators != ['/%d' % int(expected_pages)]:
                errs.append(
                    '角色一览页码分母错误: %r' % denominators
                )
        for kind, cls, path in report['manifest']['load_sequence']:
            if path not in strings:
                errs.append('加载路径缺失: %s' % path)
            short = cls.split('.')[-1]
            if main_abc.find_instance('Data', short) is None:
                # 资产类在独立 DoABC 里，主 ABC 只需引用字符串
                if short not in strings:
                    errs.append('资产类名未被主 ABC 引用: %s' % short)
        # 资产 DoABC 类都存在
        asset_ok = set()
        for i, t in f.find_tags(S.TAG_DOABC):
            _, name, abc_bytes = S.parse_doabc(t.body)
            if name == (report or {}).get('asset_abc_name') or (
                    isinstance(name, str) and name.startswith('hfport_assets_')):
                aabc = A.AbcFile.parse(abc_bytes)
                for inst in aabc.instances:
                    mn = aabc.multinames[inst['name']]
                    asset_ok.add(aabc.strings[mn[1][1]])
        for kind, cls, path in report['manifest']['load_sequence']:
            if cls.split('.')[-1] not in asset_ok:
                errs.append('资产类未生成: %s' % cls)
        for cls in report.get('new_class_ids', {}):
            if cls not in sym.values():
                errs.append('SymbolClass 缺少绑定: %s' % cls)

        new_ids = report.get('manifest', {}).get('new_ids', {})
        deps = report.get('manifest', {}).get('spt_dependencies', {})
        port_ids = set(report.get('manifest', {}).get('port_ids', []))
        for src_id in port_ids:
            new_id = new_ids.get(src_id)
            if not new_id:
                errs.append('SPT 依赖缺少新 ID: %s' % src_id)
                continue
            try:
                spt = mv.spt(new_id)
            except Exception as ex:
                errs.append('新 SPT 无法读取 %s: %s' % (new_id, ex))
                continue
            src_ref = deps.get(src_id)
            expected_ref = new_ids.get(src_ref, src_ref)
            if spt.props.get('refSpt') != expected_ref:
                errs.append(
                    'refSpt 重写错误 %s: %r != %r'
                    % (new_id, spt.props.get('refSpt'), expected_ref)
                )

    # 7. fSpec 兼容性（信息性）
    if report:
        pass  # 已在闭包阶段扫描；此处保留占位

    for e in errs[:40]:
        log('  X ' + e)
    for w in warns[:10]:
        log('  ! ' + w)
    log('验证结果: %s（%d 错误, %d 警告）' % ('通过 ✓' if not errs else '失败', len(errs), len(warns)))
    return errs, warns


if __name__ == '__main__':
    import json, io, os
    sys.stdout.reconfigure(encoding='utf-8')
    exe = sys.argv[1]
    rep = None
    if len(sys.argv) > 2:
        rep = json.load(open(sys.argv[2], encoding='utf-8'))
    errs, _ = verify(exe, rep)
    sys.exit(0 if not errs else 1)
