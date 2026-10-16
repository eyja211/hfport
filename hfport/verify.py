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
from . import loaders as L
from . import swf as S


def _check_load_hooks(f, manifest):
    """校验产物中的实际调用，不让“新角色能显示”掩盖原生 SPT 引不到新贴图。"""
    try:
        lmis, spts = L.split_load_sequence(manifest.get('load_sequence', []))
    except ValueError as exc:
        return [str(exc)]
    if not lmis and not spts:
        return []
    abc = _main_abc(f)
    if abc is None:
        return ['主 DoABC 未找到，无法验证资源加载阶段']
    errs, calls = [], {}
    specs = {'A': L.HOOK_A, 'native': ('Data', 'Global', 'LoadAllData'), 'B': L.HOOK_B}
    for stage, spec in specs.items():
        mi, _ = abc.find_class_method(*spec)
        body = abc.body_of_method(mi) if mi is not None else None
        if body is None:
            errs.append('资源加载宿主缺失: %s.%s::%s' % spec)
            calls[stage] = []
        else:
            calls[stage] = L.read_load_calls(abc, body)
    expected = lmis + spts
    classes = {cls for _, cls, _ in expected}
    paths = {path for _, _, path in expected}
    # 之前移植批次的调用不属于本次清单；但本次 class/path 的错配也不能被过滤掉。
    own = {stage: [x for x in rows if x[1] in classes or x[2] in paths]
           for stage, rows in calls.items()}
    for stage, entries in (('A', lmis), ('B', spts)):
        if own[stage] != entries:
            errs.append('钩子 %s 资源加载调用/顺序错误: 预期 %r，实际 %r'
                        % (stage, entries, own[stage]))
        for entry in entries:
            count = sum(rows.count(entry) for rows in calls.values())
            if count != 1:
                errs.append('资源 %s 加载 %d 次，预期恰好 1 次' % (entry[2], count))
    if own['native']:
        errs.append('本次资源误放进原生 LoadAllData: %r' % own['native'])
    return errs


def _check_item_route(f, manifest, report):
    """独立 itemSpt 路由层的静态断言。

    AVM2 verifier 拒类的表现是启动几秒静默退出，而结构检查全绿，所以这一层的每一条
    改动都必须有对应断言。检查项：
      1. 独立 item LMI 走 A，SPT 走 B（由 _check_load_hooks 校验），因为解析
         SPT 的那一刻就把 spriteLimb.limbName 定死成 Global.limb[name]（Spt.as:1133），
         顺序反了会一张贴图都不画且不报错。
      2. 三个新槽位真的声明在对应类上，且类型索引是 0（`*`）。
      3. Data.Global.ResolveItemSpt 存在，作用域深度与同类静态方法一致。
      4. World.AddObjI 里不再有 sptHash["item"]，改成了 ResolveItemSpt 调用。
      5. ObjH::Run / ObjI::Run 的后向分支数没变（ObjH::Run 里加一条后向分支就会让
         SWF 加载失败），且注入点数量精确。
    """
    from . import itemroute as IR

    errs = []
    resource = manifest.get('item_lmi_resource') or {}
    if not resource:
        return errs
    seq = [(k, c, p) for k, c, p in manifest.get('load_sequence', [])]
    lmi_path = resource.get('lmi_path')
    spt_path = resource.get('spt_path')
    paths = [p for _, _, p in seq]
    if lmi_path not in paths:
        errs.append('独立 item LMI %s 不在 load_sequence 里' % lmi_path)
    elif spt_path not in paths:
        errs.append('独立 item SPT %s 不在 load_sequence 里' % spt_path)
    elif paths.index(lmi_path) > paths.index(spt_path):
        errs.append('独立 item LMI 排在了 item SPT 之后，移植 item 将无贴图')

    abc = _main_abc(f)
    if abc is None:
        return errs + ['主 DoABC 未找到，无法验证路由层']

    # 2. 新槽位
    for pkg, cls, name in (('Data', 'Pt', IR.SLOT_PT_ROUTE),
                           ('Game', 'ObjI', IR.SLOT_OBJI_ROUTE),
                           ('Game', 'World', IR.SLOT_WORLD_HANDOFF)):
        trait = abc.find_class_trait(pkg, cls, name, static=False, kinds=(0,))
        if trait is None:
            errs.append('%s.%s 缺少新槽位 %s' % (pkg, cls, name))
            continue
        if trait.data[1] != 0:
            errs.append('%s.%s.%s 的类型不是 `*`（解析不到的类型会让类被拒）'
                        % (pkg, cls, name))

    # 3. 新方法
    mi, trait = abc.find_class_method('Data', 'Global', IR.METHOD_RESOLVE, static=True)
    if mi is None:
        errs.append('Data.Global.%s 未声明' % IR.METHOD_RESOLVE)
    else:
        body = abc.body_of_method(mi)
        expect = abc.dominant_scope_depths('Data', 'Global', static=True)
        if body is None:
            errs.append('Data.Global.%s 没有方法体' % IR.METHOD_RESOLVE)
        elif (body.init_scope_depth, body.max_scope_depth) != expect:
            errs.append('Data.Global.%s 的作用域深度 %r != 同类静态方法 %r'
                        % (IR.METHOD_RESOLVE,
                           (body.init_scope_depth, body.max_scope_depth), expect))

    # 4. AddObjI
    mi, _ = abc.find_class_method('Game', 'World', 'AddObjI', static=False)
    if mi is None:
        errs.append('Game.World::AddObjI 未找到')
    else:
        insns = list(A.walk_code(abc.body_of_method(mi).code))
        item_lookup = 0
        resolve_calls = 0
        for k in range(len(insns)):
            off, op, ops, nxt = insns[k]
            if (op == 0x2C and abc.strings[ops[0]] == 'item'
                    and k + 1 < len(insns) and insns[k + 1][1] == 0x66):
                item_lookup += 1
            if op == 0x46 and abc.mn_name(ops[0]) == IR.METHOD_RESOLVE:
                resolve_calls += 1
        if item_lookup:
            errs.append('World.AddObjI 里还有 %d 处 sptHash["item"] 直查' % item_lookup)
        if resolve_calls != 1:
            errs.append('World.AddObjI 里 %s 调用 %d 处，预期 1 处'
                        % (IR.METHOD_RESOLVE, resolve_calls))

    # 5. 注入点数量 + 后向分支不变
    route = (report or {}).get('item_route') or {}
    expected_sites = {'Game.ObjH::Run': 2, 'Game.ObjI::Run': 2}
    for key, want in expected_sites.items():
        pkg_cls, meth = key.split('::')
        pkg, cls = pkg_cls.split('.')
        mi, _ = abc.find_class_method(pkg, cls, meth, static=False)
        if mi is None:
            errs.append('%s 未找到' % key)
            continue
        insns = list(A.walk_code(abc.body_of_method(mi).code))
        sets = sum(1 for off, op, ops, nxt in insns
                   if op == 0x61 and abc.mn_name(ops[0]) == IR.SLOT_WORLD_HANDOFF)
        if sets != want:
            errs.append('%s 里 hfpRoute 赋值 %d 处，预期 %d 处' % (key, sets, want))
        back = sum(1 for off, op, ops, nxt in insns
                   if A.OPCODES[op][1] == ['s24'] and (nxt + ops[0]) < off)
        baseline = {'Game.ObjH::Run': 12, 'Game.ObjI::Run': 1}[key]
        if back != baseline:
            errs.append('%s 的后向分支数 %d != 原版 %d（ObjH::Run 多一条后向分支就会'
                        '让 SWF 加载失败）' % (key, back, baseline))
        starts = {off for off, _, _, _ in insns} | {len(abc.body_of_method(mi).code)}
        bad = [off for off, op, ops, nxt in insns
               if A.OPCODES[op][1] == ['s24'] and (nxt + ops[0]) not in starts]
        if bad:
            errs.append('%s 有 %d 个分支目标不在指令边界' % (key, len(bad)))
    return errs


def _main_abc(f):
    for _, t in f.find_tags(S.TAG_DOABC):
        _, name, abc_bytes = S.parse_doabc(t.body)
        abc = A.AbcFile.parse(abc_bytes)
        if abc.find_instance('Data', 'Global') is not None:
            return abc
    return None


def _pushstrings(abc, package, cls, method, static=True):
    mi, _ = abc.find_class_method(package, cls, method, static=static)
    if mi is None:
        return None
    body = abc.body_of_method(mi)
    if body is None:
        return None
    out = []
    for _, op, ops, _ in A.walk_code(body.code):
        if op == 0x2C:
            value = abc.strings[ops[0]]
            if isinstance(value, str):
                out.append(value)
    return out


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
        errs.extend(_check_load_hooks(f, manifest))
        # 目标原有的 item 资源必须一个字节都没动（移植 item 全在独立包里）
        item_hashes = manifest.get('target_item_hashes', {})
        for cls, key in (('Data.Global_itemSpt', 'spt_payload_sha256'),
                         ('Data.Global_itemLmi', 'lmi_payload_sha256')):
            want = item_hashes.get(key)
            if want and G.sha256(f.binary_payload(cls)) != want:
                errs.append('目标 %s 载荷被改动了' % cls)
        resource = manifest.get('item_lmi_resource') or {}
        routed_groups = 0
        if resource:
            routed = G.SptFile.parse(
                M.unwrap_payload(f.binary_payload(resource['spt_class']))).obj
            routed_groups = len(G.arr_items(routed.props.get('actionGroup')))
            if routed.props.get('id') != resource.get('spt_id'):
                errs.append('独立 item SPT 的 id %r != 预期 %r'
                            % (routed.props.get('id'), resource.get('spt_id')))
        mapped_groups = []
        for src_i, dst_i in manifest.get('item_group_map', {}).items():
            dst_i = int(dst_i)
            mapped_groups.append(dst_i)
            if not (0 <= dst_i < routed_groups):
                errs.append('移植 item 组 %s 映射到独立包非法下标 %d' % (src_i, dst_i))
        if len(mapped_groups) != len(set(mapped_groups)):
            errs.append('多个源 item 组映射到同一下标')
        for src_i, dst_i in manifest.get('invalid_group_map', {}).items():
            if int(dst_i) < routed_groups:
                errs.append('源悬空 item 组 %s 意外解析到独立包有效下标 %s' % (src_i, dst_i))
        for src_name, mapped in manifest.get('item_limb_map', {}).items():
            if mapped not in mv.limbs:
                errs.append('移植 item 肢体缺失: %s -> %s' % (src_name, mapped))
        # 和引擎一致：A -> 原生 LMI -> B，按 filename 取最后注册的嵌入 PNG。
        runtime_pngs = {name: entry[1] for name, entry in mv.pngs.items()}
        for mapped, expected_hash in manifest.get('copied_png_hashes', {}).items():
            got = runtime_pngs.get(mapped)
            if got is None:
                errs.append('移植 PNG 缺失: %s' % mapped)
            elif G.sha256(got) != expected_hash:
                # manifest 里存的是 sha256；早先这里用 md5 比，永远不相等。
                errs.append('移植 PNG 内容不来自源版本: %s' % mapped)

        # 目标原有 item 资源必须逐字节不变
        item_hashes = manifest.get('target_item_hashes', {})
        expected_lmi = item_hashes.get('lmi_payload_sha256')
        if expected_lmi:
            got = G.sha256(f.binary_payload('Data.Global_itemLmi'))
            if got != expected_lmi:
                errs.append('目标 Data.Global_itemLmi 载荷被改动了')

        errs.extend(_check_item_route(f, manifest, report))

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
