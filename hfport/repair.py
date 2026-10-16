# -*- coding: utf-8 -*-
"""修复模式：给**已经移植过**的角色补上正确的 item 数据，而不是再移植一遍。

场景：某个已发布的构建里，移植角色的 cre 是坏的（组号没重映射、ref 还活着被目标
Pt 表劫持），但那个构建之后又叠了别的工作（贴图重分包、手工改过的角色……），不能
推倒从干净基线重做。

与普通移植的区别：
  * **不新增角色**。角色 id / pow / charListOrder / charOption / 经验绑定 / 一览页数
    全部原样不动 —— 这些注册在上一次移植时就做过了。
  * 已发布角色 SPT 的 cre **就地按位置修复**：把源版本运行时算出的最终值内联进去、
    ref 置 -1、组号重映射到独立 item 包、打上路由标记。位置对应会先逐条校验
    （每条 cre 的 (ref, int1) 必须与源一致），不一致就拒绝，绝不盲改。
  * 只新增独立 item SPT/LMI 这一对，外加钩子 A/B 各一条加载调用和钩子 F 的路由层。
  * 角色贴图 LMI、目标 itemSpt/itemLmi、别的移植批次的资源一律不碰。

cat / ite 这两类 Pt 故意不动：实测已发布构建里 132 条 ref>=0 的 cat/ite，自身字段与
目标模板**逐字段完全相同**，劫持没有任何行为影响；动它们只会增加风险。
"""
import json
import os
import struct

from . import abcpatch as A
from . import amf as M
from . import gamedata as G
from . import itemroute as IR
from . import loaders as L
from . import porter as P
from . import swf as S


class CreRepair(object):
    def __init__(self, src_exe, dst_exe, out_exe, chars, namespace, work_dir,
                 log=print):
        self.src_exe = src_exe
        self.dst_exe = dst_exe
        self.out_exe = out_exe
        self.chars = chars
        self.namespace = P.validate_namespace(namespace)
        self.work = work_dir
        self.log = log

    # ------------------------------------------------------------------ 主流程

    def run(self):
        log = self.log
        out_abs = os.path.normcase(os.path.abspath(self.out_exe))
        for role, path in (('源版本', self.src_exe), ('目标版本', self.dst_exe)):
            if out_abs == os.path.normcase(os.path.abspath(path)):
                raise RuntimeError('输出 EXE 不能覆盖%s' % role)
        if not self.chars:
            raise RuntimeError('至少选择一个角色')
        if len(set(self.chars)) != len(self.chars):
            raise RuntimeError('角色选择中存在重复 ID')

        log('[1/7] 读取两个版本...')
        self.dst_prefix, dst_swf = S.read_projector(self.dst_exe)
        _, src_swf = S.read_projector(self.src_exe)
        self.dst = G.ModView(S.SwfFile.parse(dst_swf), 'dst')
        self.src = G.ModView(S.SwfFile.parse(src_swf), 'src')
        self.src.load_all_lmis()
        self.dst.load_all_lmis()
        log('   源肢体 %d，目标肢体 %d' % (len(self.src.limbs), len(self.dst.limbs)))
        self.container = self._selfcheck_container(dst_swf)

        log('[2/7] 校验位置对应（拒绝盲改）...')
        self.plan = P.Plan(self.chars, namespace=self.namespace)
        self.pairs = self._check_correspondence()
        log('   %d 个角色逐条 cre 的 (ref,int1) 与源一致，共 %d 条候选'
            % (len(self.pairs), sum(len(v) for v in self.pairs.values())))

        log('[3/7] 计算 item 闭包与独立包...')
        self.porter = self._build_item_pack()

        log('[4/7] 就地修复已发布角色 SPT 的 cre...')
        self.fixed_spts, self.fix_stats = self._repair_cres()

        log('[5/7] 生成补丁（钩子 A 预加载 LMI + B 加载 SPT + F 路由层）...')
        self.new_p, self.rep_p = self._payloads()
        self._patch_abc()

        log('[6/7] 组装 SWF 标签...')
        self._assemble()

        log('[7/7] 写出 EXE...')
        out = self.dst.swf.build()
        S.write_projector(self.out_exe, self.dst_prefix, out)
        log('完成: %s (%d MB)'
            % (self.out_exe, os.path.getsize(self.out_exe) // 1048576))

        report = {
            'mode': 'repair-cre',
            'src_exe': self.src_exe, 'dst_exe': self.dst_exe,
            'out_exe': self.out_exe, 'namespace': self.namespace,
            'chars': list(self.chars),
            'container_selfcheck': self.container,
            'item_route': self.item_route_report,
            'manifest': self.manifest,
            'fix_stats': self.fix_stats,
            'new_class_ids': self.new_class_ids,
            'asset_abc_name': self.asset_abc_name,
        }
        rp = os.path.join(self.work, 'repair_report.json')
        json.dump(report, open(rp, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1, default=str)
        log('修复报告: %s' % rp)
        return report

    # ------------------------------------------------------------------ 步骤

    def _selfcheck_container(self, dst_swf_bytes):
        """和普通构建同一条自检：证明容器层没丢信息。"""
        rebuilt = self.dst.swf.build()
        if rebuilt == dst_swf_bytes:
            self.log('   容器自检: 逐字节一致 ✓')
            return {'result': 'identical'}
        sig = dst_swf_bytes[:3]
        canonical = dst_swf_bytes
        if sig in (b'CWS', b'ZWS'):
            import zlib
            body = zlib.decompress(dst_swf_bytes[8:])
            canonical = (b'FWS' + dst_swf_bytes[3:4]
                         + struct.pack('<I', 8 + len(body)) + body)
        if rebuilt == canonical:
            self.log('   容器自检: 与规范化原始一致（原容器 %s）'
                     % sig.decode('latin-1'))
            return {'result': 'compression-only',
                    'orig_signature': sig.decode('latin-1')}
        raise RuntimeError('目标 SWF 容器往返丢信息，拒绝修复')

    def _check_correspondence(self):
        """逐条校验：已发布 SPT 的每个 cre 与源版本同位置、(ref,int1) 相同。

        不成立就拒绝 —— 说明这个角色被手工改过、或不是从这个源移植的，
        按位置改会改错东西。
        """
        pairs = {}
        problems = []
        for cid in self.chars:
            new_id = self.plan.new_id(cid)
            try:
                src_spt = self.src.spt(cid)
            except Exception as exc:
                problems.append('源里没有 %s: %r' % (cid, exc))
                continue
            try:
                dst_spt = self.dst.spt(new_id)
            except Exception as exc:
                problems.append('目标里没有已移植的 %s: %r' % (new_id, exc))
                continue
            s_frames = G.arr_items(src_spt.props.get('frame'))
            d_frames = G.arr_items(dst_spt.props.get('frame'))
            if len(s_frames) != len(d_frames):
                problems.append('%s 帧数不同: 源 %d / 目标 %d'
                                % (cid, len(s_frames), len(d_frames)))
                continue
            rows = []
            bad = False
            for fi, (sf, df) in enumerate(zip(s_frames, d_frames)):
                if not isinstance(sf, M.AmfObj) or not isinstance(df, M.AmfObj):
                    continue
                s_cre = [p for p in G.arr_items(sf.props.get('cre'))
                         if isinstance(p, M.AmfObj)]
                d_cre = [p for p in G.arr_items(df.props.get('cre'))
                         if isinstance(p, M.AmfObj)]
                if len(s_cre) != len(d_cre):
                    problems.append('%s 帧 %d 的 cre 数不同: %d / %d'
                                    % (cid, fi, len(s_cre), len(d_cre)))
                    bad = True
                    break
                for j, (sp, dp) in enumerate(zip(s_cre, d_cre)):
                    sref = G.ival(sp.props.get('ref'), -1)
                    dref = G.ival(dp.props.get('ref'), -1)
                    si1 = G.ival(sp.props.get('int1'), -1)
                    di1 = G.ival(dp.props.get('int1'), -1)
                    if (sref, si1) != (dref, di1):
                        problems.append(
                            '%s 帧 %d cre %d 与源不一致: 源(ref=%d,int1=%d) '
                            '目标(ref=%d,int1=%d) —— 可能被手工改过'
                            % (cid, fi, j, sref, si1, dref, di1))
                        bad = True
                        break
                    rows.append((fi, j))
                if bad:
                    break
            if not bad:
                pairs[cid] = rows
        if problems:
            raise RuntimeError('位置对应校验失败，拒绝修复：\n  '
                               + '\n  '.join(problems[:20]))
        return pairs

    def _build_item_pack(self):
        """只跑 Porter 的 item 侧：闭包 + 独立 itemSpt + 独立 item LMI + gdat 追加。"""
        porter = P.Porter(self.src, self.dst, self.plan, log=self.log)
        porter.analyze()
        porter._merge_gdat()
        porter._merge_items()
        porter._prepare_lmi_layout()
        # 角色贴图 LMI 不重做（已发布构建里那些是好的），只留 item 桶
        skipped = list(porter.out_lmi_order)
        porter.out_lmi_order = []
        porter.lmi_buckets = {}
        porter._build_lmis()
        self.log('   独立 item 包: %d 组 / %d 动作 / %d 帧 / %d 槽位；'
                 '跳过重做的角色 LMI %d 个'
                 % (len(G.arr_items(porter.out_item.props['actionGroup'])),
                    len(G.arr_items(porter.out_item.props['action'])),
                    len(G.arr_items(porter.out_item.props['frame'])),
                    len(G.arr_items(porter.out_item.props['spriteLimb'])),
                    len(skipped)))
        if porter.out_itemlmi is None:
            raise RuntimeError('没有生成独立 item LMI —— 独立 itemSpt 会引用不到 limb')
        return porter

    def _repair_cres(self):
        """按位置把每条 cre 改成源运行时的最终结果 + 路由到独立包。"""
        out = {}
        stats = {'chars': {}, 'total_fixed': 0, 'total_skipped_negative': 0}
        for cid, rows in self.pairs.items():
            new_id = self.plan.new_id(cid)
            src_spt = self.src.spt(cid)
            fixed = M.deep_clone(self.dst.spt(new_id))
            s_frames = G.arr_items(src_spt.props.get('frame'))
            d_frames = G.arr_items(fixed.props.get('frame'))
            n_fix = n_neg = 0
            for fi, j in rows:
                sp = [p for p in G.arr_items(s_frames[fi].props.get('cre'))
                      if isinstance(p, M.AmfObj)][j]
                dp = [p for p in G.arr_items(d_frames[fi].props.get('cre'))
                      if isinstance(p, M.AmfObj)][j]
                res = G.resolve_pt(self.src, sp, 'cre:%d' % fi)
                group = res.final_int1
                if group < 0:
                    n_neg += 1
                    continue
                for key in G.PT_COPY_KEYS:
                    if key in res.final.props:
                        dp.props[key] = M.deep_clone(res.final.props[key])
                dp.props['ref'] = float(-1)
                dp.props['refName'] = None
                dp.props['int1'] = float(self.porter._map_group(group))
                dp.props['itemSptRoute'] = self.namespace
                n_fix += 1
            stats['chars'][new_id] = {'fixed': n_fix, 'skipped_negative': n_neg}
            stats['total_fixed'] += n_fix
            stats['total_skipped_negative'] += n_neg
            out[new_id] = fixed
        self.log('   修好 %d 条 cre（%d 条 int1<0 不涉及 item，未动）'
                 % (stats['total_fixed'], stats['total_skipped_negative']))
        return out, stats

    def _payloads(self):
        por = self.porter
        ns = self.namespace
        new_p, rep_p = {}, {}
        item_lmi_cls = 'Data.Global_%s_itemLmi' % ns
        item_spt_cls = 'Data.Global_%s_itemSpt' % ns
        existing = set(self.dst.swf.class_to_char_id())
        for cls in (item_lmi_cls, item_spt_cls):
            if cls in existing:
                raise RuntimeError('新增资源类已存在，换一个命名空间: %s' % cls)
        new_p[item_lmi_cls] = M.wrap_payload(por.out_itemlmi.build())
        new_p[item_spt_cls] = M.wrap_payload(G.SptFile(por.out_item).build())

        rep_p['Data.Global_globalDat'] = M.wrap_payload(
            G.GdatFile(por.out_attacks, por.out_pts).build())
        for new_id, spt in self.fixed_spts.items():
            cls = 'Data.Global_%sSpt' % new_id
            if cls not in existing:
                raise RuntimeError('要替换的角色资源类不存在: %s' % cls)
            rep_p[cls] = M.wrap_payload(G.SptFile(spt).build())

        self.item_lmi_path = por.item_lmi_path
        self.item_spt_path = por.item_spt_path_planned
        self.load_sequence = [
            ('lmi', item_lmi_cls, self.item_lmi_path),
            ('spt', item_spt_cls, self.item_spt_path),
        ]
        self.manifest = {
            'namespace': ns,
            'load_sequence': self.load_sequence,
            'item_lmi_resource': {
                'namespace': ns, 'lmi_class': item_lmi_cls,
                'lmi_path': self.item_lmi_path, 'spt_class': item_spt_cls,
                'spt_path': self.item_spt_path,
                'spt_id': por.out_item.props.get('id'),
                'groups': len(G.arr_items(por.out_item.props['actionGroup'])),
                'limbs': len(por.out_itemlmi.limbs),
                'pics': len(por.out_itemlmi.limb_pics),
            },
            'item_group_map': dict(por.group_map),
            'invalid_group_map': dict(por.invalid_group_map),
            'item_limb_map': {nm: por.limb_map.get(nm, nm)
                              for nm in sorted(por.item_limb_names)},
            'replaced_classes': sorted(rep_p),
            'target_item_hashes': por.target_item_hashes,
            'warnings': por.warnings,
            'warning_digest': por.warning_digest(),
            'stats': por.stats,
        }
        self.log('   新增载荷 %d 个，替换载荷 %d 个（其中角色 SPT %d 个）'
                 % (len(new_p), len(rep_p), len(self.fixed_spts)))
        return new_p, rep_p

    def _patch_abc(self):
        tag_i = flags = name = main = None
        for i, t in self.dst.swf.find_tags(S.TAG_DOABC):
            fl, nm, ab = S.parse_doabc(t.body)
            abc = A.AbcFile.parse(ab)
            if abc.find_instance('Data', 'Global') is not None:
                tag_i, flags, name, main = i, fl, nm, abc
                break
        if tag_i is None:
            raise RuntimeError('主 DoABC 未找到')

        L.append_load_hooks(main, self.load_sequence, log=self.log)

        self.item_route_report = IR.apply_item_route(main, log=self.log)

        new_abc = main.build()
        body = struct.pack('<I', flags) + name.encode() + b'\x00' + new_abc
        self.dst.swf.tags[tag_i] = S.Tag(S.TAG_DOABC, body, long_hdr=True)

        specs = [('Data', cls.split('.')[-1], 'mx.core', 'ByteArrayAsset')
                 for cls in sorted(self.new_p)]
        self.asset_abc = A.build_asset_abc(specs)
        assert A.AbcFile.parse(self.asset_abc).build() == self.asset_abc
        self.log('   资产类 %d 个, DoABC %d 字节' % (len(specs), len(self.asset_abc)))

    def _assemble(self):
        swf = self.dst.swf
        for cls, pay in self.rep_p.items():
            swf.replace_binary(cls, pay)
        scs = swf.symbol_classes()
        main_sc_idx, _ = max(scs, key=lambda x: len(x[1]))
        nid = swf.max_char_id() + 1
        self.asset_abc_name = 'hfport_repair_%s' % self.namespace
        new_tags = [S.make_doabc_tag(self.asset_abc, self.asset_abc_name)]
        entries = []
        self.new_class_ids = {}
        for cls in sorted(self.new_p):
            new_tags.append(swf.add_binary(nid, self.new_p[cls]))
            entries.append((nid, cls))
            self.new_class_ids[cls] = nid
            nid += 1
        swf.insert_tags(main_sc_idx, new_tags)
        swf.add_symbolclass_entries(main_sc_idx + len(new_tags), entries)
        self.log('   新标签 %d 个, SymbolClass 追加 %d 条'
                 % (len(new_tags), len(entries)))
