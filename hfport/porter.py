# -*- coding: utf-8 -*-
"""移植引擎：把源 mod 的角色（含依赖闭包）合并进目标 mod 的数据层。

输出：
  - new_payloads: {类名: DefineBinaryData 载荷}（新增角色 SPT/LMI 等）
  - replaced_payloads: {类名: 载荷}（itemSpt/itemLmi/globalDat 合并结果）
  - manifest: 驱动代码补丁与验证器的注册清单
"""
import os
import re

from . import amf as M
from . import gamedata as G
from . import loaders as L

UND = M.UNDEFINED

_NS_STOP_WORDS = {'hf', 'hero', 'fighter', 'of', 'the', 'v', 'version'}
_NAMESPACE_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,31}$')


def sanitize_namespace(value):
    value = ''.join(re.findall(r'[A-Za-z0-9]+', value or '')).lower()
    if not value:
        return 'port'
    if value[0].isdigit():
        value = 'p' + value
    return value[:12]


def validate_namespace(value):
    if not isinstance(value, str) or not _NAMESPACE_RE.fullmatch(value):
        raise ValueError(
            'namespace 必须是 1-32 位 ASCII 标识符 '
            '([A-Za-z_][A-Za-z0-9_]*): %r' % (value,)
        )
    return value


def derive_namespace(exe_path):
    stem = os.path.splitext(os.path.basename(exe_path))[0]
    words = re.findall(r'[A-Za-z]+', stem)
    meaningful = [w.lower() for w in words if w.lower() not in _NS_STOP_WORDS]
    if not meaningful:
        return 'port'
    if len(meaningful) == 1:
        return sanitize_namespace(meaningful[0])
    return sanitize_namespace(''.join(w[0] for w in meaningful))


def allocate_namespace(
        base, selected_ids, occupied_ids, occupied_classes, occupied_names=()):
    base = sanitize_namespace(base)
    n = 1
    while True:
        ns = base if n == 1 else base + str(n)
        ids = {cid + '_' + ns for cid in selected_ids}
        classes = {'Data.Global_%sSpt' % cid for cid in ids}
        class_set = set(occupied_classes)
        lmi_prefix = 'Data.Global_%s_' % ns
        sound_prefix = 'PortSnd_%s_' % ns
        prefix_busy = any(
            cls.startswith(lmi_prefix) or cls.startswith(sound_prefix)
            for cls in class_set
        )
        resource_busy = any(
            isinstance(name, str) and name.endswith('@' + ns)
            for name in occupied_names
        )
        if (not (ids & set(occupied_ids))
                and not (classes & class_set)
                and not prefix_busy
                and not resource_busy):
            return ns
        n += 1


class Plan:
    def __init__(self, chars, namespace='port', display_suffix=' (Port)'):
        self.chars = chars                  # 源槽位 id 列表（不含 model0）
        self.namespace = validate_namespace(namespace)
        self.id_suffix = '_' + self.namespace
        self.name_suffix = '@' + self.namespace
        self.display_suffix = display_suffix
        self.lmi_prefix = self.namespace

    def new_id(self, slot):
        return slot + self.id_suffix


class Porter:
    def __init__(self, src, dst, plan, log=print):
        self.src = src
        self.dst = dst
        self.plan = plan
        self.log = log
        self.stats = {}
        # 决策表
        self.limb_map = {}      # 源肢体名 -> 目标肢体名（reuse 时为原名且不复制）
        self.limb_copy = {}     # 源肢体名 -> True 需要复制
        self.png_map = {}       # 源图名 -> 目标图名
        self.png_copy = set()
        self.attack_map = {}
        self.attack_copy = set()
        self.pt_map = {}
        self.pt_copy = set()
        self.pt_cre_ctx = set()
        self.group_map = {}     # 源组下标 -> 目标组下标
        self.group_copy = set()
        self.sfx_map = {}
        self.slot_map = {}      # itemSpt spriteLimb 源槽位 -> 目标槽位
        self.warnings = []
        self.invalid_group_map = {}
        self.routed_cre_count = 0
        self.dangling_groups = set()
        # 移植 item 的贴图单独装一个 LMI，目标原 itemLmi 保持原字节。路径要在
        # _merge_items 里就能引用，所以在这里定好。
        self.item_lmi_path_planned = 'lmi_data/%s_item.lmi' % self.plan.namespace
        self.item_spt_path_planned = 'spt_data/%s_item.spt' % self.plan.namespace

    # ================================================== 闭包与决策

    def analyze(self):
        src, dst, plan = self.src, self.dst, self.plan
        port_ids = self._collect_spt_dependencies(plan.chars)
        self.port_ids = port_ids

        limbs = set()
        attacks = set()
        pt_names = set()
        pt_cre = set()
        pt_idx = set()
        int1 = set()
        sfx = set()
        self.char_refs = {}
        for cid in port_ids:
            spt = src.spt(cid)
            limbs |= G.spt_limb_names(spt)
            r = G.spt_refs(spt, src)
            self.char_refs[cid] = r
            attacks |= r['attacks']
            pt_names |= r['pt_names']
            pt_cre |= r['pt_cre_names']
            pt_idx |= r['pt_indexes']
            int1 |= r['int1']
            sfx |= r['sfx']
            self.warnings.extend(self._format_pt_warning(x) for x in r['resolution_warnings'])

        nm2, cre2, bad = G.resolve_pt_indexes(src, pt_idx)
        pt_names |= nm2
        pt_cre |= cre2
        for b in bad:
            self.warnings.append('pt 数字引用无法命名: %r' % (b,))

        # pt 模板链 + 模板 int1
        pt_names, pt_cre, extra_int1 = G.expand_pt_chains(src, pt_names, pt_cre)
        int1 |= extra_int1

        # 道具闭包（迭代到不再新增：组->帧->pt->组）
        clo = G.ItemClosure(src)
        pending = set(int1)
        done_groups = set()
        for _ in range(20):
            clo.expand(pending)
            new_names, new_cre, bad = G.resolve_pt_indexes(src, clo.pt_indexes - pt_idx)
            pt_idx |= clo.pt_indexes
            for b in bad:
                self.warnings.append('道具 pt 数字引用无法命名: %r' % (b,))
            all_names, all_cre, extra = G.expand_pt_chains(
                src, pt_names | clo.pt_names | new_names, pt_cre | clo.pt_cre_names | new_cre)
            pt_names, pt_cre = all_names, all_cre
            pending = (extra | clo.groups) - clo.groups
            pending |= {g for g in extra if g not in clo.groups}
            if not pending and done_groups == clo.groups:
                break
            done_groups = set(clo.groups)
        attacks |= clo.attacks
        sfx |= clo.sfx
        attacks = G.expand_attack_chains(src, attacks)
        self.item_clo = clo
        self.warnings.extend(self._format_pt_warning(x) for x in clo.resolution_warnings)
        self.cre_resolutions = {
            cid: [x.report() for x in self.char_refs[cid]['pt_resolutions']]
            for cid in self.char_refs
        }
        self.item_cre_resolutions = [x.report() for x in clo.pt_resolutions]

        # 道具肢体（来自 itemSpt.spriteLimb 槽位）。
        # _merge_items 现在整包 1:1 复制，所以这里要收**全部**槽位的 limb，
        # 不能只收闭包用到的那几个——否则复制过来的槽位会指向不存在的 limb。
        item_slimbs = G.arr_items(src.item.props.get('spriteLimb'))
        item_limb_names = set()
        for i in range(len(item_slimbs)):
            if 0 <= i < len(item_slimbs) and isinstance(item_slimbs[i], M.AmfObj):
                sl = item_slimbs[i]
                if sl.props.get('limbName'):
                    item_limb_names.add(sl.props['limbName'])
                lns = sl.props.get('limbNames')
                if isinstance(lns, (M.AmfEcmaArray, M.AmfObj)):
                    for v in lns.props.values():
                        if isinstance(v, str) and v:
                            item_limb_names.add(v)
        limbs |= item_limb_names
        self.item_limb_names = item_limb_names

        self.need = {'limbs': limbs, 'attacks': attacks, 'pts': pt_names,
                     'groups': set(clo.groups), 'sfx': sfx}
        self.pt_cre_ctx = pt_cre

        self._decide_limbs()
        self._decide_attacks_pts()
        self._decide_groups()
        self._decide_sfx()
        self._summary()
        return self

    @staticmethod
    def _format_pt_warning(value):
        if isinstance(value, dict):
            return 'Pt 解析警告: %s' % value
        return 'Pt 解析警告: %r' % (value,)

    def warning_digest(self, samples=3):
        """把警告按种类聚成计数 + 少量样本。

        一次 22 人移植会产生上千条逐条警告（HF-EX 的 Pt 表是空的，每个 cat/ite/cre
        都会报一次 ref 越界），直接打出来会把真正值得看的那几条淹掉。
        """
        buckets = {}
        for w in self.warnings:
            kind = None
            for marker in ("{'kind': '", '"kind": "'):
                if marker in w:
                    rest = w.split(marker, 1)[1]
                    kind = 'Pt 解析警告: ' + rest.split("'", 1)[0].split('"', 1)[0]
                    break
            if kind is None:
                # 其余警告按"冒号前的说明"归类，否则每个具体下标都会自成一类。
                head = w.split(': ', 1)[0]
                kind = head if len(head) <= 40 else head[:40]
            entry = buckets.setdefault(kind, {'count': 0, 'samples': []})
            entry['count'] += 1
            if len(entry['samples']) < samples:
                entry['samples'].append(w)
        return dict(sorted(buckets.items(), key=lambda kv: -kv[1]['count']))

    def _collect_spt_dependencies(self, roots):
        """按源 SPT 的真实 refSpt 递归收集依赖，依赖排在使用者之前。"""
        visiting = set()
        placed = set()
        out = []

        def visit(cid):
            if cid in placed:
                return
            if cid in visiting:
                raise RuntimeError('源 SPT refSpt 存在非自引用循环: %s' % cid)
            try:
                spt = self.src.spt(cid)
            except Exception as ex:
                raise RuntimeError('源 SPT 依赖 "%s" 无法读取: %s' % (cid, ex))
            visiting.add(cid)
            ref = spt.props.get('refSpt')
            if isinstance(ref, str) and ref and ref != cid:
                visit(ref)
            visiting.remove(cid)
            placed.add(cid)
            out.append(cid)

        for cid in roots:
            visit(cid)
        return out

    def _decide_limbs(self):
        src, dst, sfx = self.src, self.dst, self.plan.name_suffix
        used_names = set(dst.limbs)
        for nm in sorted(self.need['limbs']):
            if nm not in src.limbs:
                # 源里就是悬空引用
                if nm in dst.limbs:
                    self.limb_map[nm] = self._fresh_name(nm, sfx + '_missing', used_names)
                    self.warnings.append('悬空肢体 %r 在目标存在，已改名保持悬空' % nm)
                else:
                    self.limb_map[nm] = nm
                continue
            force_copy = nm in self.item_limb_names
            if force_copy or nm not in dst.limbs:
                self.limb_map[nm] = self._fresh_name(nm, sfx, used_names)
                self.limb_copy[nm] = True
            elif src.limb_content_key(nm) == dst.limb_content_key(nm):
                self.limb_map[nm] = nm  # 复用目标已有
            else:
                self.limb_map[nm] = self._fresh_name(nm, sfx, used_names)
                self.limb_copy[nm] = True
        # 需要复制的肢体的图片
        for nm, need_copy in self.limb_copy.items():
            path, limb, f = src.limbs[nm]
            for idxv in G.arr_items(limb.props.get('limbPicIndex')):
                idx = G.ival(idxv)
                self._decide_pic_chain(f, idx, force=nm in self.item_limb_names)

    @staticmethod
    def _fresh_name(base, suffix, used):
        candidate = base + suffix
        n = 2
        while candidate in used:
            candidate = base + suffix + str(n)
            n += 1
        used.add(candidate)
        return candidate

    def _decide_pic_chain(self, f, idx, seen=None, force=False):
        if seen is None:
            seen = set()
        if idx < 0 or idx >= len(f.limb_pics) or idx in seen:
            return
        seen.add(idx)
        pic = f.limb_pics[idx]
        fn = pic.props.get('filename')
        if fn and (
            fn not in self.png_map
            or (force and self.png_map.get(fn) == fn)
        ):
            src, dst = self.src, self.dst
            if force:
                self.png_map[fn] = self._fresh_name(
                    fn, self.plan.name_suffix,
                    set(dst.pngs) | set(self.png_map.values())
                )
                self.png_copy.add(fn)
            elif fn not in dst.pngs:
                self.png_map[fn] = fn
                self.png_copy.add(fn)
            else:
                sp = src.pngs.get(fn)
                dp = dst.pngs.get(fn)
                same = (sp is not None and dp is not None and sp[1] == dp[1])
                if same:
                    self.png_map[fn] = fn
                    self.png_copy.add(fn)  # 载入相同字节，覆盖无害
                else:
                    self.png_map[fn] = self._fresh_name(
                        fn, self.plan.name_suffix,
                        set(dst.pngs) | set(self.png_map.values())
                    )
                    self.png_copy.add(fn)
        for refkey in ('refIndex', 'bmRefIndex'):
            r = G.ival(pic.props.get(refkey))
            if 0 <= r < len(f.limb_pics) and r != idx:
                self._decide_pic_chain(f, r, seen, force=force)

    def _decide_attacks_pts(self):
        src, dst, sfx = self.src, self.dst, self.plan.name_suffix
        used_attacks = set(dst.attack_by_name)
        for nm in sorted(self.need['attacks']):
            ent = src.attack_by_name.get(nm)
            if ent is None:
                self.attack_map[nm] = (
                    self._fresh_name(nm, sfx + '_missing', used_attacks)
                    if nm in used_attacks else nm
                )
                self.warnings.append('攻击名 %r 在源里不存在（保持运行时悬空）' % nm)
                continue
            dent = dst.attack_by_name.get(nm)
            if dent is None:
                self.attack_map[nm] = nm
                used_attacks.add(nm)
                self.attack_copy.add(nm)
            elif G.norm_obj_bytes(ent[1], drop=('i',)) == G.norm_obj_bytes(dent[1], drop=('i',)):
                self.attack_map[nm] = nm
            else:
                self.attack_map[nm] = self._fresh_name(nm, sfx, used_attacks)
                self.attack_copy.add(nm)
        used_pts = set(dst.pt_by_name)
        for nm in sorted(self.need['pts']):
            ent = src.pt_by_name.get(nm)
            if ent is None:
                self.pt_map[nm] = (
                    self._fresh_name(nm, sfx + '_missing', used_pts)
                    if nm in used_pts else nm
                )
                self.warnings.append('Pt 模板 %r 在源里不存在' % nm)
                continue
            dent = dst.pt_by_name.get(nm)
            if dent is None:
                self.pt_map[nm] = nm
                used_pts.add(nm)
                self.pt_copy.add(nm)
            elif G.norm_obj_bytes(ent[1], drop=('i',)) == G.norm_obj_bytes(dent[1], drop=('i',)):
                self.pt_map[nm] = nm
            else:
                self.pt_map[nm] = self._fresh_name(nm, sfx, used_pts)
                self.pt_copy.add(nm)

    def _decide_groups(self):
        src_groups = G.arr_items(self.src.item.props.get('actionGroup'))
        for gi in sorted(self.need['groups']):
            if gi < 0 or gi >= len(src_groups) or src_groups[gi] is None:
                self.warnings.append('源 item 组 %d 越界，保留为悬空引用' % gi)
                continue
            self.group_copy.add(gi)

    def _decide_sfx(self):
        # 音效：目标里同下标内容不同 -> 移植为新增音效
        def sound_hash(swf):
            out = {}
            for _, t in swf.find_tags(14):
                sid = t.char_id()
                out[sid] = G.md5(t.body[2:])
            return out
        # DefineSound tag id -> Console.sfx 下标 需要符号名映射
        src_syms = {}
        for _, syms in self.src.swf.symbol_classes():
            src_syms.update(syms)
        dst_syms = {}
        for _, syms in self.dst.swf.symbol_classes():
            dst_syms.update(syms)
        def sfx_index_of(name):
            # Console_mp3_021 -> 21
            if name.startswith('Console_mp3_'):
                try:
                    return int(name[len('Console_mp3_'):])
                except ValueError:
                    return None
            return None
        src_by_idx = {}
        src_name_to_tag = {nm: tid for tid, nm in src_syms.items()}
        for tid, nm in src_syms.items():
            i = sfx_index_of(nm)
            if i is not None:
                src_by_idx[i] = tid
        for i, cls in getattr(self.src, 'sfx_class_by_index', {}).items():
            if cls in src_name_to_tag:
                src_by_idx[int(i)] = src_name_to_tag[cls]
        dst_by_idx = {}
        dst_name_to_tag = {nm: tid for tid, nm in dst_syms.items()}
        for tid, nm in dst_syms.items():
            i = sfx_index_of(nm)
            if i is not None:
                dst_by_idx[i] = tid
        for i, cls in getattr(self.dst, 'sfx_class_by_index', {}).items():
            if cls in dst_name_to_tag:
                dst_by_idx[int(i)] = dst_name_to_tag[cls]
        sh_s = sound_hash(self.src.swf)
        sh_d = sound_hash(self.dst.swf)
        self.new_sounds = []
        next_new = max(dst_by_idx) + 1 if dst_by_idx else 87
        for i in sorted(self.need['sfx']):
            ts, td = src_by_idx.get(i), dst_by_idx.get(i)
            if ts is None:
                self.sfx_map[i] = i
                if i not in dst_by_idx:
                    self.warnings.append('sfx %d 在两版都找不到符号映射' % i)
                continue
            if td is not None and sh_s.get(ts) == sh_d.get(td):
                self.sfx_map[i] = i
            else:
                self.sfx_map[i] = next_new
                self.new_sounds.append({'src_tag': ts, 'sfx_index': next_new,
                                        'class': 'PortSnd_%s_%d' % (
                                            self.plan.namespace, i
                                        ), 'orig_index': i})
                next_new += 1

    def _summary(self):
        st = self.stats
        st['limbs_total'] = len(self.need['limbs'])
        st['limbs_copy'] = len(self.limb_copy)
        st['limbs_renamed'] = sum(1 for k, v in self.limb_map.items() if k != v)
        st['pngs_copy'] = len(self.png_copy)
        st['pngs_renamed'] = sum(1 for k, v in self.png_map.items() if k != v)
        st['attacks_copy'] = len(self.attack_copy)
        st['attacks_renamed'] = sum(1 for k, v in self.attack_map.items() if k != v)
        st['pts_copy'] = len(self.pt_copy)
        st['groups_total'] = len(self.need['groups'])
        st['groups_copy'] = len(self.group_copy)
        st['new_sounds'] = len(getattr(self, 'new_sounds', []))
        self.log('闭包决策: %s' % st)
        digest = self.warning_digest()
        if digest:
            self.log('  警告合计 %d 条，按种类：' % len(self.warnings))
            for kind, info in digest.items():
                self.log('    %5d x %s' % (info['count'], kind))
                self.log('          例: %s' % info['samples'][0])

    # ================================================== 变换与合并

    def transform(self):
        # gdat 先合并：_merge_items 里的 item 帧也要按名字把 cat/ite 的 Pt 模板重指
        # 到目标索引，这需要 out_pt_indices 已经就绪。
        self._merge_gdat()
        self._merge_items()
        self._prepare_lmi_layout()
        self._build_char_spts()
        self._build_lmis()
        return self

    # ---- gdat ----

    def _merge_gdat(self):
        src, dst = self.src, self.dst
        self.out_attacks = [a for a in dst.gdat.attacks]
        self.out_pts = [p for p in dst.gdat.pts]
        self.attack_new_idx = {}
        for nm in sorted(self.attack_copy):
            a = M.deep_clone(src.attack_by_name[nm][1])
            a.props['name'] = self.attack_map[nm]
            self.attack_new_idx[self.attack_map[nm]] = len(self.out_attacks)
            self.out_attacks.append(a)
        self.pt_new_idx = {}
        for nm in sorted(self.pt_copy):
            p = M.deep_clone(src.pt_by_name[nm][1])
            p.props['name'] = self.pt_map[nm]
            self.pt_new_idx[self.pt_map[nm]] = len(self.out_pts)
            self.out_pts.append(p)
        # 修 copied 条目的 ref/refName 链到目标索引
        dst_atk_idx = {a.props.get('name'): i for i, a in enumerate(self.out_attacks) if a.props.get('name')}
        dst_pt_idx = {p.props.get('name'): i for i, p in enumerate(self.out_pts) if p.props.get('name')}
        self.out_pt_indices = dict(dst_pt_idx)
        for nm in self.attack_copy:
            a = self.out_attacks[self.attack_new_idx[self.attack_map[nm]]]
            self._fix_named_ref(a, self.src.gdat.attacks, self.attack_map, dst_atk_idx)
        for nm in self.pt_copy:
            p = self.out_pts[self.pt_new_idx[self.pt_map[nm]]]
            self._fix_named_ref(p, self.src.gdat.pts, self.pt_map, dst_pt_idx)

    def _fix_named_ref(self, obj, src_list, name_map, dst_idx):
        """copied 条目内部的 ref/refName -> 目标名字与目标索引。"""
        rn = obj.props.get('refName')
        ref = G.ival(obj.props.get('ref'))
        if rn:
            mapped = name_map.get(rn, rn)
            obj.props['refName'] = mapped
            if mapped in dst_idx:
                obj.props['ref'] = float(dst_idx[mapped])
        elif 0 <= ref < len(src_list):
            nm2 = src_list[ref].props.get('name')
            if nm2:
                mapped = name_map.get(nm2, nm2)
                obj.props['refName'] = mapped
                if mapped in dst_idx:
                    obj.props['ref'] = float(dst_idx[mapped])
            else:
                obj.props['ref'] = float(-1)
                self.warnings.append('copied 条目数字 ref 指向无名条目，已置 -1')

    def _map_group(self, gi):
        """源组号 -> 独立包组号。整包 1:1 复制，所以是恒等映射。

        源里本来就越界/指向空组的组号（例如 HFEP 的 skadi 有 int1=598，而 HFEP 只有
        589 组）**原样保留** —— 这才是忠实复制"源里这一发就是打不出东西"。引擎
        `Obj.SetA` 对 `tempA == null` 有兜底（回落到组 0 并 trace），不会崩。
        """
        if gi in self.group_map:
            return self.group_map[gi]
        self.dangling_groups.add(gi)
        return gi

    # ---- itemSpt / itemLmi ----

    def _merge_items(self):
        """构造独立 item SPT：**下标与源 1:1 全量复制**，只改名字和跨包引用。

        为什么不做密排重编号（这是踩过的坑）：
        `ObjI::Run` 会在动作帧之外**按 fi 偏移**去取旋转/斜向变体（`ai == C._45 ||
        ai == C._90` 那条分支里 `++fi`），也就是说"某个动作 last 帧之后紧跟着什么帧"
        是引擎依赖的数据，不只是数组顺序。密排以后 easonball 飞行动作 last 帧后面
        原本是 6 帧 ball1/ball2 变体，被换成了爆炸帧 —— 玩家看到的就是飞行贴图和爆炸
        贴图不断交替。同理 action 下标（`actionIndex` 里可能出现同一 action 多次）和
        组下标（子道具靠 `int1`）都保持原值最安全。

        代价只是带上一些用不到的组：source 全量 90 组 vs 闭包 57 组，载荷差十几 KB。
        贴图不多花——limb 本来就是整条 `limbPicIndex` 复制的。
        """
        src = self.src
        s_groups = G.arr_items(src.item.props.get('actionGroup'))
        s_actions = G.arr_items(src.item.props.get('action'))
        s_frames = G.arr_items(src.item.props.get('frame'))
        s_slots = G.arr_items(src.item.props.get('spriteLimb'))

        self.out_item = M.deep_clone(src.item)
        self.out_item.props['id'] = 'item_%s' % self.plan.namespace
        self.out_item.props['name'] = 'Item@%s' % self.plan.namespace
        # refSpt 只有在帧带 rsn/rsi 时才被读（Spt.as:1166-1209）。
        rs = src.item.props.get('refSpt')
        if isinstance(rs, str) and rs and rs not in self.dst.spts_available():
            if rs in self.port_ids:
                self.out_item.props['refSpt'] = self.plan.new_id(rs)
            else:
                self.warnings.append(
                    '独立 item SPT 的 refSpt %r 在目标里不存在且未被移植，已清空' % rs)
                self.out_item.props['refSpt'] = ''
        self.out_item.props['lmiFiles'] = G.new_ecma([self.item_lmi_path_planned])

        self.target_item_counts = {
            'groups': len(G.arr_items(self.dst.item.props.get('actionGroup'))),
            'actions': len(G.arr_items(self.dst.item.props.get('action'))),
            'frames': len(G.arr_items(self.dst.item.props.get('frame'))),
            'slots': len(G.arr_items(self.dst.item.props.get('spriteLimb'))),
        }

        # 1:1 恒等映射 —— 保留给下游（_fix_frame_refs / _map_group）使用。
        self.slot_map = {i: i for i in range(len(s_slots))}
        self.group_map = {i: i for i, g in enumerate(s_groups)
                          if isinstance(g, M.AmfObj)}
        self.invalid_group_map = {}
        self.item_action_map = {i: i for i in range(len(s_actions))}
        self.item_frame_map = {i: i for i in range(len(s_frames))}

        d_slots = G.arr_items(self.out_item.props['spriteLimb'])
        for sl in d_slots:
            if isinstance(sl, M.AmfObj):
                self._map_spritelimb(sl)

        d_frames = G.arr_items(self.out_item.props['frame'])
        for fr in d_frames:
            if isinstance(fr, M.AmfObj):
                self._fix_frame_refs(fr, item_ctx=True)

        d_actions = G.arr_items(self.out_item.props['action'])
        # a_keyTgr.agi 是组号，恒等映射下无需改写，仍做一次范围校验。
        for ai, act in enumerate(d_actions):
            if not isinstance(act, M.AmfObj):
                continue
            for kt in G.arr_items(act.props.get('a_keyTgr')):
                if isinstance(kt, M.AmfObj):
                    agi = G.ival(kt.props.get('agi'), -1)
                    if agi >= 0 and agi not in self.group_map:
                        self.warnings.append(
                            'item action %d 的 a_keyTgr.agi=%d 指向空组' % (ai, agi))

        d_groups = G.arr_items(self.out_item.props['actionGroup'])
        self.group_name_map = {}
        self.unused_piece_warns = 0
        for gi, g in enumerate(d_groups):
            if not isinstance(g, M.AmfObj):
                continue
            nm = g.props.get('name')
            if isinstance(nm, str) and nm:
                # 组名保持源版本原样：独立包的名字进不了 iteHash
                # （GenerateIteHash 只遍历 sptHash["item"]），顶不掉目标同名组。
                self.group_name_map[gi] = (nm, nm)
            for key in ('ite_atkName1', 'ite_atkName2'):
                val = g.props.get(key)
                if val:
                    g.props[key] = self.attack_map.get(val, val)
            # 闭包内的组是真的会被调用的，Piece.i 必须能唯一映射；闭包外的组
            # （整包复制带过来但没人 route 到）映射不了就保留源值 —— 引擎在
            # Spt.LoadFromBytes 里有 `piece.i < globalSpt.action.length` 的范围
            # 检查，越界只 trace 一句。但绝不能写成负数：action[-1] 是 undefined，
            # 再取 .frameIndex 会抛异常，而那里被 catch(Error) 吞掉、静默中止后续加载。
            used = gi in self.group_copy
            for piece in G.arr_items(g.props.get('piece')):
                if not isinstance(piece, M.AmfObj):
                    continue
                try:
                    self._map_piece_action(piece, gi, nm)
                except RuntimeError:
                    if used:
                        raise
                    self.unused_piece_warns += 1
                    if self.unused_piece_warns <= 5:
                        self.warnings.append(
                            '未被调用的 item 组 %d(%s) 的 Piece.i=%s 无法映射，'
                            '保留源值' % (gi, nm, G.ival(piece.props.get('i'), -1)))

        self.target_item_hashes = {
            'spt_payload_sha256': G.sha256(
                self.dst.swf.binary_payload('Data.Global_itemSpt')
            ),
            'lmi_payload_sha256': G.sha256(
                self.dst.swf.binary_payload('Data.Global_itemLmi')
            ),
        }
        self.stats['item_copy_mode'] = 'verbatim-1to1'
        self.stats['unused_piece_unmapped'] = self.unused_piece_warns
        self.stats['item_groups_copied'] = len(self.group_map)
        self.stats['item_groups_in_closure'] = len(self.group_copy)
        self.stats['item_actions_copied'] = sum(
            1 for a in d_actions if isinstance(a, M.AmfObj))
        self.stats['item_frames_copied'] = sum(
            1 for x in d_frames if isinstance(x, M.AmfObj))
        self.stats['item_slots_copied'] = len(d_slots)
        self.stats['routed_item_tables'] = {
            'groups': len(d_groups), 'actions': len(d_actions),
            'frames': len(d_frames), 'slots': len(d_slots),
        }

    def _map_piece_action(self, piece, group_index, group_name):
        """把 Piece.i 从源 global spt 的动作下标重指到目标 global spt 的同名动作。

        Spt.LoadFromBytes 对 ITEM 类型的 spt 会做
        ``piece.fi = Global.sptHash["global"].action[piece.i].frameIndex``，
        所以 Piece.i 必须是**目标** global spt 的合法下标。
        """
        src_i = G.ival(piece.props.get('i'), -1)
        if src_i < 0:
            return
        dst_i = self._resolve_global_action(src_i, group_index, group_name)
        piece.props['i'] = float(dst_i)
        target_actions = G.arr_items(self.dst.spt('global').props.get('action'))
        if 0 <= dst_i < len(target_actions) and isinstance(target_actions[dst_i], M.AmfObj):
            piece.props['fi'] = target_actions[dst_i].props.get('frameIndex', -1.0)

    def _resolve_global_action(self, src_i, group_index, group_name):
        """按动作名唯一匹配。名字才是身份，帧内容不是。

        早先这里额外要求源/目标动作的帧链内容签名一致，结果 rock 的
        ROCK_PIECE_BIG、iceLand 的 ICE_PIECE1..3 全部匹配失败并让构建中止 —— 两边
        动作同名同下标，只是帧美术不同。帧内容不一致只记警告。
        """
        if not hasattr(self, '_global_action_map'):
            self._global_action_map = {}
            self.global_action_notes = []
        if src_i in self._global_action_map:
            return self._global_action_map[src_i]
        src_spt = self.src.spt('global')
        dst_spt = self.dst.spt('global')
        src_actions = G.arr_items(src_spt.props.get('action'))
        dst_actions = G.arr_items(dst_spt.props.get('action'))
        if not (0 <= src_i < len(src_actions)) or not isinstance(src_actions[src_i], M.AmfObj):
            raise RuntimeError(
                'item Piece.i=%d 越过源 global action 数组 (group=%s/%s)'
                % (src_i, group_index, group_name)
            )
        source_name = src_actions[src_i].props.get('name')
        if not isinstance(source_name, str) or not source_name:
            raise RuntimeError(
                'item Piece.i=%d 指向无名的源 global 动作 (group=%s/%s)'
                % (src_i, group_index, group_name)
            )
        candidates = [
            dst_i for dst_i, candidate in enumerate(dst_actions)
            if isinstance(candidate, M.AmfObj)
            and candidate.props.get('name') == source_name
        ]
        if len(candidates) != 1:
            raise RuntimeError(
                'item Piece.i=%d 无法在目标 global spt 里唯一定位动作 %r '
                '(group=%s/%s, candidates=%r)'
                % (src_i, source_name, group_index, group_name, candidates)
            )
        dst_i = candidates[0]
        same = (self._global_action_signature(src_spt, src_i)
                == self._global_action_signature(dst_spt, dst_i))
        note = {
            'src_index': src_i, 'dst_index': dst_i, 'name': source_name,
            'frames_identical': same,
        }
        self.global_action_notes.append(note)
        if not same:
            self.warnings.append(
                'item Piece 动作 %r: 源 %d -> 目标 %d 同名但帧内容不同，'
                '碎片美术会用目标版本' % (source_name, src_i, dst_i)
            )
        self._global_action_map[src_i] = dst_i
        return dst_i

    @staticmethod
    def _global_action_signature(spt, action_index):
        actions = G.arr_items(spt.props.get('action'))
        frames = G.arr_items(spt.props.get('frame'))
        action = actions[action_index]
        stable_action = G.norm_obj_bytes(
            action, drop=('index', 'frame', 'frameIndex')
        )
        chain = []
        for fi in G.frames_of_action(frames, action):
            frame = M.deep_clone(frames[fi])
            for key in ('index', 'refIndex', 'refIndex_2', 'ref', 'ref_2',
                        'limbPose', 'lmat'):
                frame.props.pop(key, None)
            chain.append(G.norm_obj_bytes(frame))
        return G.md5(stable_action + b'|' + b'|'.join(chain))

    @staticmethod
    def _sequence_hash(items):
        return G.md5(b'|'.join(G.norm_obj_bytes(x) for x in items))

    def _map_spritelimb(self, sl):
        nm = sl.props.get('limbName')
        if isinstance(nm, str) and nm:
            sl.props['limbName'] = self.limb_map.get(nm, nm)
        lns = sl.props.get('limbNames')
        if isinstance(lns, (M.AmfEcmaArray, M.AmfObj)):
            for k, v in list(lns.props.items()):
                if isinstance(v, str) and v:
                    lns.props[k] = self.limb_map.get(v, v)

    def _fix_frame_refs(self, fr, item_ctx):
        """帧内跨文件引用重写：攻击名 / pt / cre.int1 / sfx / (道具)uz槽位。"""
        for key in ('attack', 'attackB', 'editAttack', 'editAttackB'):
            for box in G.arr_items(fr.props.get(key)):
                if isinstance(box, M.AmfObj):
                    rn = box.props.get('refName')
                    if rn:
                        box.props['refName'] = self.attack_map.get(rn, rn)
        for ctx, pt in G.frame_pts(fr):
            resolution = G.resolve_pt(self.src, pt, ctx)
            for warning in resolution.warnings:
                self.warnings.append(self._format_pt_warning(warning))
            final_group = resolution.final_int1

            if ctx == 'cre' and final_group >= 0:
                # 这里是三个 item bug 的共同修复点。移植过来的 cre 绝不能再让目标
                # 的 Global.pt 插手：把源运行时算出的最终值内联进 cre，把 ref 置
                # -1 让 CheckCopyRefPt 跳过模板分支，再把组号重映射到独立包的局部
                # 下标，并打上路由标记指向独立 itemSpt。
                #
                # 旧行为保留了 ref>=0，于是目标模板整片覆盖 int1/vx/vy/vz/facing/
                # int2/int3：saws 的 ref=142(int1=38) 被 Eyja 的 cre_sawsground
                # 覆盖成 78、ref=143(int1=78) 被覆盖成 38（两个技能互换），
                # lucas2/Azrael 的 ref=147/149(int1=80) 全被覆盖成 45
                # (thunder_big_ball)。
                for key in G.PT_COPY_KEYS:
                    if key in resolution.final.props:
                        pt.props[key] = M.deep_clone(resolution.final.props[key])
                pt.props['ref'] = float(-1)
                pt.props['refName'] = None
                pt.props['int1'] = float(self._map_group(final_group))
                pt.props['itemSptRoute'] = self.plan.namespace
                self.routed_cre_count += 1
                continue

            rn = pt.props.get('refName')
            raw_ref = G.ival(pt.props.get('ref'), -1)
            mapped_template = False

            # Prefer a named target Pt when the source template is provable.
            template_name = (resolution.template.props.get('name')
                              if isinstance(resolution.template, M.AmfObj)
                              else None)
            if template_name and template_name in self.pt_map:
                mapped_name = self.pt_map[template_name]
                target_index = getattr(self, 'out_pt_indices', {}).get(mapped_name)
                if target_index is not None and raw_ref != -1:
                    pt.props['refName'] = mapped_name
                    pt.props['ref'] = float(target_index)
                    mapped_template = True
            elif rn:
                # A refName with ref=-1 is intentionally direct and must stay
                # direct; the runtime does not resolve the name in that case.
                pt.props['refName'] = self.pt_map.get(rn, rn)

            if raw_ref >= 0 and not mapped_template:
                # A numeric source ref without a unique target template is
                # de-templated. Inline the source runtime result so a target
                # Pt at the same numeric index can never hijack this cre.
                for key in G.PT_COPY_KEYS:
                    if key in resolution.final.props:
                        pt.props[key] = M.deep_clone(resolution.final.props[key])
                pt.props['ref'] = float(-1)

        sfx = G.ival(fr.props.get('sfx'))
        if sfx >= 0 and sfx in self.sfx_map and self.sfx_map[sfx] != sfx:
            fr.props['sfx'] = float(self.sfx_map[sfx])
        if item_ctx:
            for key in ('uz', 'lz'):
                for z in G.arr_items(fr.props.get(key)):
                    if isinstance(z, M.AmfObj):
                        i = G.ival(z.props.get('i'))
                        if i < 0:
                            continue
                        if i not in self.slot_map:
                            raise RuntimeError('item 帧引用未闭包 spriteLimb 槽位 %d' % i)
                        z.props['i'] = float(self.slot_map[i])
            for key in ('upLimbs', 'lowLimbs'):
                arr = fr.props.get(key)
                if arr is not None and arr is not UND:
                    items = G.arr_items(arr)
                    if items:
                        mapped = []
                        for value in items:
                            if not isinstance(value, (int, float)):
                                mapped.append(value)
                                continue
                            src_slot = G.ival(value)
                            if src_slot < 0:
                                mapped.append(value)
                                continue
                            if src_slot not in self.slot_map:
                                raise RuntimeError(
                                    'item 帧 %s 引用未闭包 spriteLimb 槽位 %d' %
                                    (key, src_slot)
                                )
                            mapped.append(float(self.slot_map[src_slot]))
                        G.arr_set(arr, mapped)

    # ---- 角色 SPT ----

    def _build_char_spts(self):
        self.out_spts = {}
        port_set = set(self.port_ids)
        for cid in self.port_ids:
            spt = M.deep_clone(self.src.spt(cid))
            new_id = self.plan.new_id(cid)
            spt.props['id'] = new_id
            rs = spt.props.get('refSpt')
            if isinstance(rs, str) and rs in port_set:
                spt.props['refSpt'] = self.plan.new_id(rs)
            # lmiFiles 重写
            lf = spt.props.get('lmiFiles')
            if lf is not None and lf is not UND:
                items = G.arr_items(lf)
                G.arr_set(lf, [self._map_lmi_path(x) if isinstance(x, str) else x for x in items])
            for sl in G.arr_items(spt.props.get('spriteLimb')):
                if isinstance(sl, M.AmfObj):
                    self._map_spritelimb(sl)
            for fr in G.arr_items(spt.props.get('frame')):
                if isinstance(fr, M.AmfObj):
                    self._fix_frame_refs(fr, item_ctx=False)
            self.out_spts[cid] = spt

    def _map_lmi_path(self, path):
        p = path.replace('\\', '/')
        return self.lmi_path_map.get(p, path)

    def _prepare_lmi_layout(self):
        """按真实源 LMI 文件分桶，并按角色第一次引用顺序保留加载顺序。"""
        source_buckets = {}
        self.item_bucket = sorted(
            nm for nm in self.item_limb_names if nm in self.limb_copy
        )
        item_set = set(self.item_bucket)
        for nm in sorted(self.limb_copy):
            if nm in item_set:
                continue
            source_path, _, _ = self.src.limbs[nm]
            source_buckets.setdefault(source_path, []).append(nm)

        source_order = []
        seen = set()
        for cid in self.port_ids:
            for value in G.arr_items(self.src.spt(cid).props.get('lmiFiles')):
                if not isinstance(value, str):
                    continue
                path = value.replace('\\', '/')
                if path in source_buckets and path not in seen:
                    seen.add(path)
                    source_order.append(path)
        source_order.extend(sorted(set(source_buckets) - seen))

        used_paths = set(self.dst.lmis)
        self.lmi_path_map = {}
        self.lmi_buckets = {}
        self.out_lmi_order = []
        self.item_lmi_path = self.item_lmi_path_planned
        if self.item_bucket:
            if self.item_lmi_path in used_paths:
                raise RuntimeError('独立 item LMI 路径已存在: %s' % self.item_lmi_path)
            used_paths.add(self.item_lmi_path)
            self.lmi_path_map['lmi_data/item.lmi'] = self.item_lmi_path
        for source_path in source_order:
            stem = source_path[len('lmi_data/'):-4]
            safe_stem = re.sub(r'[^A-Za-z0-9_]+', '_', stem).strip('_') or 'data'
            base = '%s_%s' % (self.plan.lmi_prefix, safe_stem)
            candidate = 'lmi_data/%s.lmi' % base
            n = 2
            while candidate in used_paths:
                candidate = 'lmi_data/%s%d.lmi' % (base, n)
                n += 1
            used_paths.add(candidate)
            self.lmi_path_map[source_path] = candidate
            self.lmi_buckets[candidate] = source_buckets[source_path]
            self.out_lmi_order.append(candidate)

    # ---- LMI 构建 ----

    def _build_lmis(self):
        """按源文件分桶构建角色 LMI，并独立构造 item LMI。"""
        self.lmi_warnings = []
        self.png_winner_resolutions = []
        self.out_lmis = {}
        for path in self.out_lmi_order:
            names = self.lmi_buckets[path]
            self.out_lmis[path] = self._make_lmi_file(names)

        self.out_itemlmi = None
        if self.item_bucket:
            self.out_itemlmi = self._make_lmi_file(self.item_bucket)
        self._validate_output_lmis()
        self.warnings.extend(self.lmi_warnings)
        self.stats['out_lmi_files'] = {p: (len(f.limbs), len(f.limb_pics))
                                       for p, f in self.out_lmis.items()}
        if self.out_itemlmi:
            self.stats['itemlmi_final'] = (len(self.out_itemlmi.limbs), len(self.out_itemlmi.limb_pics))

    def _make_lmi_file(self, names):
        f = G.LmiFile()
        self._append_limbs(f, names)
        return f

    def _append_limbs(self, out, names):
        """把源肢体（含图链）追加进 out，并保持源 LimbPic 的相对顺序。"""
        src = self.src
        needed = {}    # (source_path, source_index) -> source LmiFile

        def collect_pic(path, sf, idx):
            key = (path, idx)
            if idx < 0 or idx >= len(sf.limb_pics) or key in needed:
                return
            needed[key] = sf
            pic = sf.limb_pics[idx]
            for refkey in ('refIndex', 'bmRefIndex'):
                r = G.ival(pic.props.get(refkey))
                if r == idx:
                    self.lmi_warnings.append(
                        'LMI 图片 %s[%d] 的 %s 自引用，按运行时置空' %
                        (path, idx, refkey)
                    )
                    continue
                if r >= 0:
                    if r >= len(sf.limb_pics):
                        self.lmi_warnings.append(
                            'LMI 图片 %s[%d] 的 %s=%d 越界，按运行时置空' %
                            (path, idx, refkey, r)
                        )
                        continue
                    collect_pic(path, sf, r)

        for nm in names:
            path, limb, sf = src.limbs[nm]
            for value in G.arr_items(limb.props.get('limbPicIndex')):
                idx = G.ival(value)
                if idx >= 0:
                    collect_pic(path, sf, idx)

        ordered = sorted(needed)
        pic_pos = {
            key: len(out.limb_pics) + offset
            for offset, key in enumerate(ordered)
        }
        for path, idx in ordered:
            sf = needed[(path, idx)]
            pic = M.deep_clone(sf.limb_pics[idx])
            raw_refs = {
                key: G.ival(pic.props.get(key), -1)
                for key in ('refIndex', 'bmRefIndex')
            }
            pos = pic_pos[(path, idx)]
            pic.props['index'] = float(pos)
            fn = pic.props.get('filename')
            if isinstance(fn, str) and fn:
                pic.props['filename'] = self.png_map.get(fn, fn)
            for refkey in ('refIndex', 'bmRefIndex'):
                r = raw_refs[refkey]
                if r == idx:
                    pic.props[refkey] = float(-1)
                    continue
                if (path, r) in pic_pos:
                    pic.props[refkey] = float(pic_pos[(path, r)])
                elif r >= 0:
                    if r >= len(sf.limb_pics):
                        pic.props[refkey] = float(-1)
                    else:
                        raise RuntimeError(
                            'LMI 图片 %s[%d] 的 %s=%d 未进入闭包' %
                            (path, idx, refkey, r)
                        )
            embedded = bool(pic.props.get('embeded'))
            png = None
            if embedded:
                winner = src.pngs.get(fn) if isinstance(fn, str) else None
                if winner is None or winner[1] is None:
                    raise RuntimeError(
                        'LMI 图片 %s[%d] embeded=true 但源运行时没有 PNG winner' %
                        (path, idx)
                    )
                png = winner[1]
                winner_pic = winner[2]
                for key in ('cx', 'cy', 'r0'):
                    if key in winner_pic.props:
                        pic.props[key] = M.deep_clone(winner_pic.props[key])
                own = sf.pngs[idx]
                if own is None or own != png:
                    self.png_winner_resolutions.append({
                        'filename': fn,
                        'source_path': path,
                        'source_index': idx,
                        'winner_path': winner[0],
                        'winner_index': winner[3],
                        'winner_sha256': G.sha256(png),
                    })
            out.limb_pics.append(pic)
            out.pngs.append(png)

        for nm in names:
            sp, limb, sf = src.limbs[nm]
            c = M.deep_clone(limb)
            c.props['name'] = self.limb_map[nm]
            lpi = c.props.get('limbPicIndex')
            if lpi is not None and lpi is not UND:
                items = G.arr_items(lpi)
                mapped = []
                for x in items:
                    xi = G.ival(x)
                    if xi >= 0:
                        mapped.append(float(pic_pos.get((sp, xi), -1)))
                    else:
                        mapped.append(x)
                G.arr_set(lpi, mapped)
            # limbPic 数组（运行时由 limbPicIndex 重建）置空防串
            if 'limbPic' in c.props:
                lp = c.props['limbPic']
                if isinstance(lp, (M.AmfEcmaArray, M.AmfStrictArray)):
                    G.arr_set(lp, [None] * len(G.arr_items(lp)))
            out.limbs.append(c)

    def _validate_output_lmis(self):
        files = []
        if self.out_itemlmi is not None:
            files.append((self.item_lmi_path, self.out_itemlmi))
        files.extend((path, self.out_lmis[path]) for path in self.out_lmi_order)

        limb_owner = {name: path for name, (path, _, _) in self.dst.limbs.items()}
        filename_hash = {
            name: G.sha256(entry[1]) for name, entry in self.dst.pngs.items()
            if entry[1] is not None
        }
        for path, lmi in files:
            if len(lmi.limb_pics) != len(lmi.pngs):
                raise RuntimeError('%s 的 limbPic/PNG 数量不一致' % path)
            for index, (pic, png) in enumerate(zip(lmi.limb_pics, lmi.pngs)):
                if G.ival(pic.props.get('index'), -1) != index:
                    raise RuntimeError('%s limbPic[%d].index 不连续' % (path, index))
                embedded = bool(pic.props.get('embeded'))
                if embedded != (png is not None):
                    raise RuntimeError(
                        '%s limbPic[%d] embeded 与 PNG 存在性不一致' %
                        (path, index)
                    )
                for key in ('refIndex', 'bmRefIndex'):
                    ref = G.ival(pic.props.get(key), -1)
                    if ref >= len(lmi.limb_pics) or ref == index:
                        raise RuntimeError(
                            '%s limbPic[%d].%s=%d 非法' %
                            (path, index, key, ref)
                        )
                filename = pic.props.get('filename')
                if embedded and not filename:
                    raise RuntimeError('%s limbPic[%d] 缺少 filename' % (path, index))
                if embedded:
                    digest = G.sha256(png)
                    old = filename_hash.get(filename)
                    if old is not None and old != digest:
                        raise RuntimeError(
                            '全局 PNG filename winner 冲突 %r: %s != %s' %
                            (filename, old, digest)
                        )
                    filename_hash[filename] = digest
            for limb in lmi.limbs:
                name = limb.props.get('name')
                if not name:
                    raise RuntimeError('%s 含无名 limb' % path)
                if name in limb_owner:
                    raise RuntimeError(
                        'limb 名称跨 LMI 冲突 %r: %s / %s' %
                        (name, limb_owner[name], path)
                    )
                limb_owner[name] = path
                for value in G.arr_items(limb.props.get('limbPicIndex')):
                    index = G.ival(value, -1)
                    if index >= len(lmi.limb_pics):
                        raise RuntimeError(
                            '%s limb %r 的 limbPicIndex=%d 越界' %
                            (path, name, index)
                        )

    # ================================================== 载荷输出

    def payloads(self):
        """返回 (new_payloads, replaced_payloads, manifest骨架)"""
        new_p = {}
        rep_p = {}
        gd = G.GdatFile(self.out_attacks, self.out_pts)
        rep_p['Data.Global_globalDat'] = M.wrap_payload(gd.build())

        load_seq = []
        lmi_classes = {}
        item_resource = None
        if self.out_itemlmi is not None:
            item_lmi_cls = 'Data.Global_%s_itemLmi' % self.plan.namespace
            item_spt_cls = 'Data.Global_%s_itemSpt' % self.plan.namespace
            new_p[item_lmi_cls] = M.wrap_payload(self.out_itemlmi.build())
            new_p[item_spt_cls] = M.wrap_payload(G.SptFile(self.out_item).build())
            lmi_classes[self.item_lmi_path] = item_lmi_cls
            # LMI 必须早于所有原生 SPT，不能只早于自己那个独立 SPT：解析 SPT 时
            # 就把 spriteLimb.limbName 定死成 Global.limb[name]（Spt.as:1133），
            # 之后再注册的 limb 对它永远不可见，且不报任何错。
            load_seq.append(('lmi', item_lmi_cls, self.item_lmi_path))
            load_seq.append(('spt', item_spt_cls, self.item_spt_path_planned))
            item_resource = {
                'namespace': self.plan.namespace,
                'lmi_class': item_lmi_cls,
                'lmi_path': self.item_lmi_path,
                'spt_class': item_spt_cls,
                'spt_path': self.item_spt_path_planned,
                'spt_id': self.out_item.props.get('id'),
                'spt_hash_key': self.out_item.props.get('id'),
                'groups': len(G.arr_items(self.out_item.props['actionGroup'])),
                'limbs': len(self.out_itemlmi.limbs),
                'pics': len(self.out_itemlmi.limb_pics),
                'routed_cre': self.routed_cre_count,
            }
        elif self.group_copy:
            raise RuntimeError(
                '有 %d 个 item 组要移植，但没有生成独立 item LMI —— '
                '独立 itemSpt 会引用不到任何 limb' % len(self.group_copy))

        for path in self.out_lmi_order:
            f = self.out_lmis[path]
            stem = path[len('lmi_data/'):-4]
            cls = 'Data.Global_%sLmi' % stem
            if cls in new_p:
                raise RuntimeError('新增资源类冲突: %s' % cls)
            new_p[cls] = M.wrap_payload(f.build())
            lmi_classes[path] = cls
            load_seq.append(('lmi', cls, path))

        # _collect_spt_dependencies 已保证非自引用依赖在前。
        order = []
        placed = set()
        for cid in self.port_ids:
            spt = self.out_spts[cid]
            rs = spt.props.get('refSpt')
            dep = None
            for other in self.port_ids:
                if self.plan.new_id(other) == rs:
                    dep = other
                    break
            if dep and dep != cid and dep not in placed:
                raise RuntimeError(
                    'refSpt 加载顺序错误: %s 依赖尚未加载的 %s' % (cid, dep)
                )
            order.append(cid)
            placed.add(cid)

        for cid in order:
            cls = 'Data.Global_%sSpt' % self.plan.new_id(cid)
            new_p[cls] = M.wrap_payload(G.SptFile(self.out_spts[cid]).build())
            load_seq.append(('spt', cls, 'spt_data/%s.spt' % self.plan.new_id(cid)))

        # 清单与运行时相同：A 的全部 LMI 在前，B 的 SPT 保持 refSpt 拓扑序。
        lmi_loads, spt_loads = L.split_load_sequence(load_seq)
        load_seq = lmi_loads + spt_loads

        mapped_png_names = set(self.png_map.values())
        copied_png_hashes = {}
        png_files = []
        if self.out_itemlmi is not None:
            png_files.append(self.out_itemlmi)
        png_files.extend(self.out_lmis[path] for path in self.out_lmi_order)
        for lmi in png_files:
            for idx, pic in enumerate(lmi.limb_pics):
                filename = pic.props.get('filename')
                if filename in mapped_png_names and lmi.pngs[idx] is not None:
                    digest = G.sha256(lmi.pngs[idx])
                    old = copied_png_hashes.get(filename)
                    if old is not None and old != digest:
                        raise RuntimeError('输出 PNG filename 内容冲突: %s' % filename)
                    copied_png_hashes[filename] = digest

        target_item_hashes = {
            'spt_class': 'Data.Global_itemSpt',
            'spt_payload_sha256': G.sha256(
                self.dst.swf.binary_payload('Data.Global_itemSpt')
            ),
            'lmi_class': 'Data.Global_itemLmi',
            'lmi_payload_sha256': G.sha256(
                self.dst.swf.binary_payload('Data.Global_itemLmi')
            ),
        }
        payload_hashes = {
            cls: G.sha256(payload) for cls, payload in sorted(new_p.items())
        }
        payload_hashes.update(
            (cls, G.sha256(payload)) for cls, payload in sorted(rep_p.items())
        )

        manifest = {
            'namespace': self.plan.namespace,
            'chars': [cid for cid in self.plan.chars],
            'new_ids': {cid: self.plan.new_id(cid) for cid in self.port_ids},
            'port_ids': list(self.port_ids),
            'spt_dependencies': {
                cid: self.src.spt(cid).props.get('refSpt') for cid in self.port_ids
            },
            'load_sequence': load_seq,
            'item_lmi_resource': item_resource,
            'new_sounds': self.new_sounds,
            'sfx_map': self.sfx_map,
            'item_group_map': dict(self.group_map),
            'item_group_name_map': dict(getattr(self, 'group_name_map', {})),
            'invalid_group_map': dict(self.invalid_group_map),
            'item_action_map': dict(self.item_action_map),
            'item_frame_map': dict(self.item_frame_map),
            'item_slot_map': dict(self.slot_map),
            'global_action_map': dict(getattr(self, '_global_action_map', {})),
            'global_action_notes': list(getattr(self, 'global_action_notes', [])),
            'item_limb_map': {
                nm: self.limb_map.get(nm, nm) for nm in sorted(self.item_limb_names)
            },
            'copied_png_hashes': copied_png_hashes,
            'target_item_counts': self.target_item_counts,
            'target_item_hashes': target_item_hashes,
            'routed_cre_count': self.routed_cre_count,
            'dangling_item_groups': sorted(self.dangling_groups),
            'payload_hashes': payload_hashes,
            'source_lmi_load_order': list(self.src.lmi_load_order),
            'png_winner_resolutions': list(self.png_winner_resolutions),
            'cre_resolutions': self.cre_resolutions,
            'item_cre_resolutions': self.item_cre_resolutions,
            'stats': self.stats,
            'warnings': self.warnings,
            'warning_digest': self.warning_digest(),
        }
        return new_p, rep_p, manifest
