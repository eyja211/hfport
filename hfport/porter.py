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

UND = M.UNDEFINED

_NS_STOP_WORDS = {'hf', 'hero', 'fighter', 'of', 'the', 'v', 'version'}


def sanitize_namespace(value):
    value = ''.join(re.findall(r'[A-Za-z0-9]+', value or '')).lower()
    if not value:
        return 'port'
    if value[0].isdigit():
        value = 'p' + value
    return value[:12]


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
        self.namespace = sanitize_namespace(namespace)
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
            r = G.spt_refs(spt)
            self.char_refs[cid] = r
            attacks |= r['attacks']
            pt_names |= r['pt_names']
            pt_cre |= r['pt_cre_names']
            pt_idx |= r['pt_indexes']
            int1 |= r['int1']
            sfx |= r['sfx']

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

        # 道具肢体（来自 itemSpt.spriteLimb 槽位）
        item_slimbs = G.arr_items(src.item.props.get('spriteLimb'))
        item_limb_names = set()
        for i in clo.limb_slots:
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
        for w in self.warnings[:12]:
            self.log('  警告: %s' % w)
        if len(self.warnings) > 12:
            self.log('  ...共 %d 条警告' % len(self.warnings))

    # ================================================== 变换与合并

    def transform(self):
        self._merge_items()      # 先合并道具（分配组下标供 gdat/角色 int1 重映射）
        self._merge_gdat()
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
            if nm in self.pt_cre_ctx:
                i1 = G.ival(p.props.get('int1'), -1)
                if i1 >= 0:
                    p.props['int1'] = float(self._map_group(i1))
            self.pt_new_idx[self.pt_map[nm]] = len(self.out_pts)
            self.out_pts.append(p)
        # 修 copied 条目的 ref/refName 链到目标索引
        dst_atk_idx = {a.props.get('name'): i for i, a in enumerate(self.out_attacks) if a.props.get('name')}
        dst_pt_idx = {p.props.get('name'): i for i, p in enumerate(self.out_pts) if p.props.get('name')}
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
        got = self.group_map.get(gi)
        if got is None:
            if gi in self.invalid_group_map:
                return self.invalid_group_map[gi]
            self.warnings.append('int1 组 %d 不在闭包内，未重映射' % gi)
            return gi
        return got

    # ---- itemSpt / itemLmi ----

    def _merge_items(self):
        src, dst = self.src, self.dst
        self.out_item = M.deep_clone(dst.item)
        d_groups = G.arr_items(self.out_item.props.get('actionGroup'))
        d_actions = G.arr_items(self.out_item.props.get('action'))
        d_frames = G.arr_items(self.out_item.props.get('frame'))
        d_slots = G.arr_items(self.out_item.props.get('spriteLimb'))
        s_groups = G.arr_items(src.item.props.get('actionGroup'))
        s_actions = G.arr_items(src.item.props.get('action'))
        s_frames = G.arr_items(src.item.props.get('frame'))
        s_slots = G.arr_items(src.item.props.get('spriteLimb'))

        # 1) spriteLimb 槽位映射
        for i in sorted(self.item_clo.limb_slots):
            if not (0 <= i < len(s_slots)) or not isinstance(s_slots[i], M.AmfObj):
                self.slot_map[i] = i
                continue
            c = M.deep_clone(s_slots[i])
            self._map_spritelimb(c)
            self.slot_map[i] = len(d_slots)
            d_slots.append(c)

        # 2) 分配复制组的目标下标
        next_g = len(d_groups)
        for gi in sorted(self.group_copy):
            self.group_map[gi] = next_g
            next_g += 1
        for offset, gi in enumerate(sorted(self.need['groups'] - self.group_copy)):
            self.invalid_group_map[gi] = next_g + offset

        # 3) 收集复制组的动作/帧集合（含 refIndex 链）
        copy_actions = set()
        copy_frames = set()

        def visit_frame(fi):
            if fi in copy_frames or not (0 <= fi < len(s_frames)) or s_frames[fi] is None:
                return
            copy_frames.add(fi)
            fr = s_frames[fi]
            for key in ('refIndex', 'refIndex_2'):
                r = G.ival(fr.props.get(key), 0)
                if r > 0:
                    visit_frame(r)

        for gi in sorted(self.group_copy):
            g = s_groups[gi]
            if g is None:
                continue
            for cat in G.arr_items(g.props.get('actionIndex')):
                for aiv in G.arr_items(cat):
                    ai = G.ival(aiv)
                    if 0 <= ai < len(s_actions) and s_actions[ai] is not None:
                        copy_actions.add(ai)
                        for fi in G.frames_of_action(s_frames, s_actions[ai]):
                            visit_frame(fi)

        action_map = {}
        for ai in sorted(copy_actions):
            action_map[ai] = len(d_actions) + len(action_map)
        frame_map = {}
        for fi in sorted(copy_frames):
            frame_map[fi] = len(d_frames) + len(frame_map)

        # 4) 复制并重写帧
        for fi in sorted(copy_frames):
            fr = M.deep_clone(s_frames[fi])
            fr.props['index'] = float(frame_map[fi])
            for key in ('refIndex', 'refIndex_2'):
                r = G.ival(fr.props.get(key), 0)
                if r > 0:
                    fr.props[key] = float(frame_map.get(r, 0))
            self._fix_frame_refs(fr, item_ctx=True)
            d_frames.append(fr)

        # 5) 复制并重写动作
        for ai in sorted(copy_actions):
            act = M.deep_clone(s_actions[ai])
            fi = G.ival(act.props.get('frameIndex'))
            if fi in frame_map:
                act.props['frameIndex'] = float(frame_map[fi])
            akt = act.props.get('a_keyTgr')
            if akt is not None and akt is not UND:
                for kt in G.arr_items(akt):
                    if isinstance(kt, M.AmfObj):
                        agi = G.ival(kt.props.get('agi'))
                        if agi in self.group_map:
                            kt.props['agi'] = float(self.group_map[agi])
            d_actions.append(act)

        # 6) 复制并重写组
        used_group_names = {
            group.props.get('name')
            for group in d_groups
            if isinstance(group, M.AmfObj) and group.props.get('name')
        }
        for gi in sorted(self.group_copy):
            g = M.deep_clone(s_groups[gi])
            g.props['index'] = float(self.group_map[gi])
            nm = g.props.get('name')
            if isinstance(nm, str) and nm:
                g.props['name'] = self._fresh_name(
                    nm, self.plan.name_suffix, used_group_names
                )
            ktr = G.ival(g.props.get('keyTriggerRefIndex'))
            if ktr >= 0:
                g.props['keyTriggerRefIndex'] = float(self._map_group(ktr))
            for key in ('ite_atkName1', 'ite_atkName2'):
                v = g.props.get(key)
                if v:
                    g.props[key] = self.attack_map.get(v, v)
            ai_arr = g.props.get('actionIndex')
            if ai_arr is not None and ai_arr is not UND:
                for cat in G.arr_items(ai_arr):
                    if isinstance(cat, (M.AmfEcmaArray, M.AmfStrictArray)):
                        items = G.arr_items(cat)
                        G.arr_set(cat, [float(action_map.get(G.ival(x), G.ival(x)))
                                        if isinstance(x, (int, float)) else x for x in items])
            d_groups.append(g)

        G.arr_set(self.out_item.props['actionGroup'], d_groups)
        G.arr_set(self.out_item.props['action'], d_actions)
        G.arr_set(self.out_item.props['frame'], d_frames)
        G.arr_set(self.out_item.props['spriteLimb'], d_slots)
        target_counts = {
            'groups': len(G.arr_items(self.dst.item.props.get('actionGroup'))),
            'actions': len(G.arr_items(self.dst.item.props.get('action'))),
            'frames': len(G.arr_items(self.dst.item.props.get('frame'))),
            'slots': len(G.arr_items(self.dst.item.props.get('spriteLimb'))),
        }
        self.item_prefix_hashes = {
            'groups': self._sequence_hash(d_groups[:target_counts['groups']]),
            'actions': self._sequence_hash(d_actions[:target_counts['actions']]),
            'frames': self._sequence_hash(d_frames[:target_counts['frames']]),
            'slots': self._sequence_hash(d_slots[:target_counts['slots']]),
        }
        self.target_item_counts = target_counts
        self.stats['item_actions_copied'] = len(copy_actions)
        self.stats['item_frames_copied'] = len(copy_frames)
        self.stats['item_slots_added'] = len(d_slots) - len(G.arr_items(self.dst.item.props.get('spriteLimb')))

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
            rn = pt.props.get('refName')
            ref = G.ival(pt.props.get('ref'))
            if rn:
                pt.props['refName'] = self.pt_map.get(rn, rn)
            elif ref >= 0:
                # 数字模板引用 -> 回填名字（按目标名），保持 ref>=0 让加载器重解析
                if 0 <= ref < len(self.src.gdat.pts):
                    nm = self.src.gdat.pts[ref].props.get('name')
                    if nm:
                        pt.props['refName'] = self.pt_map.get(nm, nm)
                    else:
                        self.warnings.append('帧 pt 数字引用指向无名模板 #%d' % ref)
            if ctx == 'cre':
                i1 = G.ival(pt.props.get('int1'), -1)
                if i1 >= 0 and ref < 0:
                    pt.props['int1'] = float(self._map_group(i1))
        sfx = G.ival(fr.props.get('sfx'))
        if sfx >= 0 and sfx in self.sfx_map and self.sfx_map[sfx] != sfx:
            fr.props['sfx'] = float(self.sfx_map[sfx])
        if item_ctx:
            for key in ('uz', 'lz'):
                for z in G.arr_items(fr.props.get(key)):
                    if isinstance(z, M.AmfObj):
                        i = G.ival(z.props.get('i'))
                        if i in self.slot_map and self.slot_map[i] != i:
                            z.props['i'] = float(self.slot_map[i])
            for key in ('upLimbs', 'lowLimbs'):
                arr = fr.props.get(key)
                if arr is not None and arr is not UND:
                    items = G.arr_items(arr)
                    if items:
                        G.arr_set(arr, [float(self.slot_map.get(G.ival(x), G.ival(x)))
                                        if isinstance(x, (int, float)) else x for x in items])

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
        self.item_bucket = []
        for nm in sorted(self.limb_copy):
            source_path, _, _ = self.src.limbs[nm]
            if source_path == 'lmi_data/item.lmi':
                self.item_bucket.append(nm)
            else:
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
        """按 _prepare_lmi_layout 的源文件分桶构建 LMI；item 合并进目标 itemLmi。"""
        self.out_lmis = {}
        for path in self.out_lmi_order:
            names = self.lmi_buckets[path]
            self.out_lmis[path] = self._make_lmi_file(names)

        # itemLmi 合并
        self.out_itemlmi = None
        if self.item_bucket:
            base = self.dst.lmis['lmi_data/item.lmi']
            merged = G.LmiFile(
                [M.deep_clone(p) for p in base.limb_pics],
                list(base.pngs),
                [M.deep_clone(l) for l in base.limbs])
            self._append_limbs(merged, self.item_bucket)
            self.out_itemlmi = merged
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
                if 0 <= r < len(sf.limb_pics) and r != idx:
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
            pos = pic_pos[(path, idx)]
            pic.props['index'] = float(pos)
            fn = pic.props.get('filename')
            if isinstance(fn, str) and fn:
                pic.props['filename'] = self.png_map.get(fn, fn)
            for refkey in ('refIndex', 'bmRefIndex'):
                r = G.ival(pic.props.get(refkey))
                if (path, r) in pic_pos:
                    pic.props[refkey] = float(pic_pos[(path, r)])
                elif r >= 0:
                    pic.props[refkey] = float(-1)
            out.limb_pics.append(pic)
            out.pngs.append(sf.pngs[idx])

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

    # ================================================== 载荷输出

    def payloads(self):
        """返回 (new_payloads, replaced_payloads, manifest骨架)"""
        new_p = {}
        rep_p = {}
        gd = G.GdatFile(self.out_attacks, self.out_pts)
        rep_p['Data.Global_globalDat'] = M.wrap_payload(gd.build())
        rep_p['Data.Global_itemSpt'] = M.wrap_payload(G.SptFile(self.out_item).build())
        if self.out_itemlmi is not None:
            rep_p['Data.Global_itemLmi'] = M.wrap_payload(self.out_itemlmi.build())

        load_seq = []
        lmi_classes = {}
        for path in self.out_lmi_order:
            f = self.out_lmis[path]
            stem = path[len('lmi_data/'):-4]
            cls = 'Data.Global_%sLmi' % stem
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
                    copied_png_hashes[filename] = G.md5(lmi.pngs[idx])

        manifest = {
            'chars': [cid for cid in self.plan.chars],
            'new_ids': {cid: self.plan.new_id(cid) for cid in self.port_ids},
            'port_ids': list(self.port_ids),
            'spt_dependencies': {
                cid: self.src.spt(cid).props.get('refSpt') for cid in self.port_ids
            },
            'load_sequence': load_seq,
            'new_sounds': self.new_sounds,
            'sfx_map': self.sfx_map,
            'item_group_map': dict(self.group_map),
            'invalid_group_map': dict(self.invalid_group_map),
            'item_limb_map': {
                nm: self.limb_map.get(nm, nm) for nm in sorted(self.item_limb_names)
            },
            'copied_png_hashes': copied_png_hashes,
            'target_item_counts': self.target_item_counts,
            'item_prefix_hashes': self.item_prefix_hashes,
            'stats': self.stats,
            'warnings': self.warnings,
        }
        return new_p, rep_p, manifest
