# -*- coding: utf-8 -*-
"""HF 游戏数据模型 + 移植引擎：闭包 -> 改名/重映射 -> 合并 -> 新载荷。

原则：只改"移植副本"。目标默认 itemSpt/itemLmi 保持原字节，迁移 item
进入独立的 routed SPT/LMI；只有 globalDat 会按依赖闭包追加。
"""
import hashlib
from . import amf as M
from . import swf as S

UND = M.UNDEFINED


def arr_items(v):
    """ECMA/严格数组 -> 按数字键排序的值列表。"""
    if v is None or v is UND:
        return []
    if isinstance(v, M.AmfStrictArray):
        return v.items
    ks = [k for k in v.props if k.lstrip('-').isdigit()]
    return [v.props[k] for k in sorted(ks, key=int)]


def arr_set(v, items):
    """用 items 重写 ECMA 数组的数字键（保持 AmfEcmaArray 形态）。"""
    if isinstance(v, M.AmfStrictArray):
        v.items = list(items)
        return v
    for k in [k for k in v.props if k.lstrip('-').isdigit()]:
        del v.props[k]
    for i, x in enumerate(items):
        v.props[str(i)] = x
    v.count = len(items)
    return v


def new_ecma(items):
    a = M.AmfEcmaArray(len(items))
    for i, x in enumerate(items):
        a.props[str(i)] = x
    return a


def ival(v, dflt=-1):
    return int(v) if isinstance(v, (int, float)) else dflt


PT_COPY_KEYS = ('facing', 'vx', 'vy', 'vz', 'int1', 'int2', 'int3')


class PtResolution:
    """One Pt resolved with the runtime CheckCopyRefPt semantics."""

    def __init__(self, original, final, template=None, template_index=-1,
                 route=None, status='direct', warnings=None):
        self.original = original
        self.final = final
        self.template = template
        self.template_index = template_index
        self.route = route
        self.status = status
        self.warnings = list(warnings or ())

    @property
    def final_int1(self):
        return ival(self.final.props.get('int1'), -1)

    @property
    def effective_ref(self):
        return ival(self.final.props.get('ref'), -1)

    def report(self):
        keys = ('refName', 'ref', 'facing', 'vx', 'vy', 'vz',
                'int1', 'int2', 'int3', 'itemSptRoute')
        return {
            'original': {k: self.original.props.get(k) for k in keys},
            'template_index': self.template_index,
            'template_name': (self.template.props.get('name')
                              if isinstance(self.template, M.AmfObj) else None),
            'final': {k: self.final.props.get(k) for k in keys},
            'route': self.route,
            'status': self.status,
            'warnings': list(self.warnings),
        }


def resolve_pt(mod, pt, context=''):
    """Resolve one Pt exactly as Data.Spt.CheckCopyRefPt does."""
    if not isinstance(pt, M.AmfObj):
        raise TypeError('Pt must be AmfObj, got %r' % (type(pt),))
    original = M.deep_clone(pt)
    final = M.deep_clone(pt)
    route = pt.props.get('itemSptRoute')
    raw_ref = ival(pt.props.get('ref'), -1)
    effective_ref = raw_ref
    status = 'direct'
    warnings = []

    # This is the runtime refName/ref branch.
    if pt.props.get('refName') is not None and raw_ref != -1:
        name = pt.props.get('refName')
        ent = mod.pt_by_name.get(name) if isinstance(name, str) else None
        if ent is None:
            warnings.append({
                'kind': 'pt-ref-name-missing',
                'context': context,
                'refName': name,
                'ref': raw_ref,
            })
            status = 'unresolved-refName'
        else:
            effective_ref = ent[0]
            status = 'refName-resolved'

    pts = mod.gdat.pts
    if 0 <= effective_ref < len(pts):
        template = pts[effective_ref]
        for key in PT_COPY_KEYS:
            if key in template.props:
                final.props[key] = M.deep_clone(template.props[key])
        return PtResolution(
            original, final, template=template, template_index=effective_ref,
            route=route, status='template' if status == 'direct' else status,
            warnings=warnings,
        )

    if effective_ref >= 0:
        warnings.append({
            'kind': 'pt-ref-out-of-range',
            'context': context,
            'refName': pt.props.get('refName'),
            'ref': effective_ref,
            'pt_count': len(pts),
        })
        status = 'invalid-ref'
    return PtResolution(
        original, final, template=None, template_index=-1, route=route,
        status=status, warnings=warnings,
    )


def resolve_pt_fields(mod, pt, context=''):
    """Return the final CopyRefAttributes fields and the full resolution."""
    res = resolve_pt(mod, pt, context)
    return {k: res.final.props.get(k) for k in PT_COPY_KEYS}, res


def md5(b):
    return hashlib.md5(b).hexdigest()


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def norm_obj_bytes(obj, drop=()):
    if obj is None:
        return b'null'
    clone = M.AmfObj(obj.alias)
    for k in sorted(obj.props):
        if k in drop:
            continue
        clone.props[k] = obj.props[k]
    return M.amf0_encode(clone)


# ------------------------------------------------------------ 文件模型

class SptFile:
    def __init__(self, obj):
        self.obj = obj

    @classmethod
    def parse(cls, stream):
        sr = M.StreamReader(stream)
        if sr.utf() != 'Spt':
            raise ValueError('bad Spt header')
        obj = M.amf0_decode(sr.obj_bytes())
        if not sr.eof():
            raise ValueError('trailing bytes in Spt stream')
        return cls(obj)

    def build(self):
        sw = M.StreamWriter()
        sw.utf('Spt')
        sw.obj_bytes(M.amf0_encode(self.obj))
        return bytes(sw.out)


class LmiFile:
    def __init__(self, limb_pics=None, pngs=None, limbs=None):
        self.limb_pics = limb_pics if limb_pics is not None else []
        self.pngs = pngs if pngs is not None else []
        self.limbs = limbs if limbs is not None else []

    @classmethod
    def parse(cls, stream):
        sr = M.StreamReader(stream)
        if sr.utf() != 'limbInfo':
            raise ValueError('bad lmi header')
        n = sr.i32()
        pics = [M.amf0_decode(sr.obj_bytes()) for _ in range(n)]
        pngs = [sr.obj_bytes() for _ in range(n)]
        m = sr.i32()
        limbs = [M.amf0_decode(sr.obj_bytes()) for _ in range(m)]
        if not sr.eof():
            raise ValueError('trailing bytes in lmi stream')
        return cls(pics, pngs, limbs)

    def build(self):
        sw = M.StreamWriter()
        sw.utf('limbInfo')
        sw.i32(len(self.limb_pics))
        for p in self.limb_pics:
            sw.obj_bytes(M.amf0_encode(p))
        for b in self.pngs:
            sw.obj_bytes(b)
        sw.i32(len(self.limbs))
        for l in self.limbs:
            sw.obj_bytes(M.amf0_encode(l))
        return bytes(sw.out)


class GdatFile:
    def __init__(self, attacks, pts):
        self.attacks = attacks
        self.pts = pts

    @classmethod
    def parse(cls, stream):
        sr = M.StreamReader(stream)
        if sr.utf() != 'gdat':
            raise ValueError('bad gdat header')
        n = sr.i32()
        attacks = [M.amf0_decode(sr.obj_bytes()) for _ in range(n)]
        m = sr.i32()
        pts = [M.amf0_decode(sr.obj_bytes()) for _ in range(m)]
        if not sr.eof():
            raise ValueError('trailing bytes in gdat stream')
        return cls(attacks, pts)

    def build(self):
        sw = M.StreamWriter()
        sw.utf('gdat')
        sw.i32(len(self.attacks))
        for a in self.attacks:
            sw.obj_bytes(M.amf0_encode(a))
        sw.i32(len(self.pts))
        for p in self.pts:
            sw.obj_bytes(M.amf0_encode(p))
        return bytes(sw.out)


# ------------------------------------------------------------ mod 视图

SPT_OF = lambda cid: 'Data.Global_%sSpt' % cid
LMI_OF = lambda cid: 'Data.Global_%sLmi' % cid


class ModView:
    """一个已加载 mod 的数据视图（基于 SwfFile）。"""

    def __init__(self, swf_file, name=''):
        self.swf = swf_file
        self.name = name
        self.lmis = {}       # 'lmi_data/x.lmi' -> LmiFile
        self.limbs = {}      # limb name -> (lmi_path, limb_obj, LmiFile)
        self.pngs = {}       # pic filename -> (lmi_path, png_bytes, pic_obj)
        self.lmi_load_order = []
        self.spts = {}
        self.gdat = GdatFile.parse(M.unwrap_payload(self.swf.binary_payload('Data.Global_globalDat')))
        self.attack_by_name = {}
        for i, a in enumerate(self.gdat.attacks):
            nm = a.props.get('name')
            if nm:
                self.attack_by_name[nm] = (i, a)
        self.pt_by_name = {}
        for i, p in enumerate(self.gdat.pts):
            nm = p.props.get('name')
            if nm:
                self.pt_by_name[nm] = (i, p)
        self.item = self.spt('item')

    def lmi_path_of_class(self, cls):
        return 'lmi_data/%s.lmi' % cls[len('Data.Global_'):-3]

    def load_all_lmis(self):
        parsed = {}
        for cls in sorted(self.swf.class_to_char_id()):
            if cls.startswith('Data.Global_') and cls.endswith('Lmi'):
                path = self.lmi_path_of_class(cls)
                if path in parsed:
                    continue
                f = LmiFile.parse(M.unwrap_payload(self.swf.binary_payload(cls)))
                parsed[path] = f

        declared = self._declared_lmi_load_order()
        order = [path for path in declared if path in parsed]
        order.extend(sorted(set(parsed) - set(order)))
        self.lmi_load_order = order
        self.lmis = {path: parsed[path] for path in order}
        self.limbs = {}
        self.pngs = {}
        for path in order:
            f = parsed[path]
            for i, pic in enumerate(f.limb_pics):
                fn = pic.props.get('filename')
                if fn and f.pngs[i] is not None and pic.props.get('embeded'):
                    self.pngs[fn] = (path, f.pngs[i], pic, i)
            for limb in f.limbs:
                nm = limb.props.get('name')
                if nm:
                    self.limbs[nm] = (path, limb, f)

    def _declared_lmi_load_order(self):
        """Read the runtime LMI path order from the main ABC load methods."""
        from . import abcpatch as A

        out = []
        seen = set()
        method_specs = (
            ('Data', 'Global', 'LoadGlobalDatFromCompressedBytes', True),
            ('Data', 'Global', 'LoadAllData', True),
            ('Data', 'BgInfoFile', 'LoadFromCompressedBytes', True),
        )
        for _, tag in self.swf.find_tags(S.TAG_DOABC):
            try:
                _, _, abc_bytes = S.parse_doabc(tag.body)
                abc = A.AbcFile.parse(abc_bytes)
            except Exception:
                continue
            for package, cls_name, method_name, static in method_specs:
                method, _ = abc.find_class_method(
                    package, cls_name, method_name, static=static
                )
                body = abc.body_of_method(method) if method is not None else None
                if body is None:
                    continue
                for _, op, operands, _ in A.walk_code(body.code):
                    if op != 0x2C or not operands:
                        continue
                    value = abc.strings[operands[0]]
                    if (isinstance(value, str)
                            and value.startswith('lmi_data/')
                            and value.endswith('.lmi')
                            and value not in seen):
                        seen.add(value)
                        out.append(value)
        return out

    def spt(self, cid):
        if cid not in self.spts:
            self.spts[cid] = SptFile.parse(M.unwrap_payload(self.swf.binary_payload(SPT_OF(cid)))).obj
        return self.spts[cid]

    def spts_available(self):
        """这个版本里存在哪些 SPT id（按 Data.Global_<id>Spt 资源类推断）。"""
        out = set()
        for cls in self.swf.class_to_char_id():
            if cls.startswith('Data.Global_') and cls.endswith('Spt'):
                out.add(cls[len('Data.Global_'):-3])
        return out

    # ---- 内容签名（判定"同名同内容"可复用）----

    def limb_content_key(self, limb_name):
        path, limb, f = self.limbs[limb_name]
        parts = [norm_obj_bytes(limb, drop=('limbPicIndex',))]
        for idx in arr_items(limb.props.get('limbPicIndex')):
            if not isinstance(idx, (int, float)):
                parts.append(b'nil')
                continue
            idx = int(idx)
            if idx < 0 or idx >= len(f.limb_pics):
                parts.append(b'bad')
                continue
            parts.append(self.pic_content_key(f, idx))
        return md5(b'|'.join(parts))

    def pic_content_key(self, f, idx, seen=None):
        if seen is None:
            seen = set()
        if idx in seen:
            return b'cycle'
        seen.add(idx)
        pic = f.limb_pics[idx]
        parts = [norm_obj_bytes(pic, drop=('index', 'refIndex', 'bmRefIndex'))]
        png = f.pngs[idx]
        parts.append(png if png is not None else b'nopng')
        for refkey in ('refIndex', 'bmRefIndex'):
            r = ival(pic.props.get(refkey))
            if 0 <= r < len(f.limb_pics) and r != idx:
                parts.append(self.pic_content_key(f, r, seen))
        return b'/'.join([b'pic'] + parts)


# ------------------------------------------------------------ 引用遍历

def frame_pts(frame):
    """yield (context, pt)；context in {'cat','rid','ite','cre'}"""
    for key in ('cat', 'rid', 'ite'):
        pt = frame.props.get(key)
        if isinstance(pt, M.AmfObj):
            yield key, pt
    for pt in arr_items(frame.props.get('cre')):
        if isinstance(pt, M.AmfObj):
            yield 'cre', pt


def frames_of_action(spt_frames, action, cap=2000):
    """action.frameIndex 起到 last:true 的帧下标序列。"""
    fi = ival(action.props.get('frameIndex'))
    out = []
    n = 0
    while 0 <= fi < len(spt_frames) and n < cap:
        fr = spt_frames[fi]
        if fr is None:
            break
        out.append(fi)
        if fr.props.get('last'):
            break
        fi += 1
        n += 1
    return out


class ItemClosure:
    """道具组闭包：groups + 涉及的 actions/frames + 外部引用。"""

    def __init__(self, mod):
        self.mod = mod
        self.groups = set()
        self.actions = set()
        self.frames = set()
        self.attacks = set()
        self.pt_names = set()
        self.pt_cre_names = set()  # 被 cre 上下文用到的 pt 模板（int1 需重映射）
        self.pt_indexes = set()
        self.sfx = set()
        self.limb_slots = set()    # itemSpt.spriteLimb 下标
        self.pt_resolutions = []
        self.resolution_warnings = []

    def expand(self, seeds):
        item = self.mod.item
        groups = arr_items(item.props.get('actionGroup'))
        actions = arr_items(item.props.get('action'))
        frames = arr_items(item.props.get('frame'))
        todo = set(int(s) for s in seeds)
        while todo:
            gi = todo.pop()
            if gi in self.groups:
                continue
            self.groups.add(gi)
            if gi < 0 or gi >= len(groups) or groups[gi] is None:
                continue
            g = groups[gi]
            for key in ('ite_atkName1', 'ite_atkName2'):
                v = g.props.get(key)
                if v:
                    self.attacks.add(v)
            for key in ('ite_sfx1', 'ite_sfx2'):
                v = ival(g.props.get(key), -1)
                if v > 0:
                    self.sfx.add(v)
            ktr = ival(g.props.get('keyTriggerRefIndex'))
            if ktr >= 0:
                todo.add(ktr)
            for cat in arr_items(g.props.get('actionIndex')):
                for aiv in arr_items(cat):
                    ai = ival(aiv)
                    if not (0 <= ai < len(actions)) or actions[ai] is None:
                        continue
                    if ai in self.actions:
                        continue
                    self.actions.add(ai)
                    akt = actions[ai].props.get('a_keyTgr')
                    for trigger in arr_items(akt):
                        if not isinstance(trigger, M.AmfObj):
                            continue
                        agi = ival(trigger.props.get('agi'), -1)
                        if agi >= 0:
                            todo.add(agi)
                    for fi in frames_of_action(frames, actions[ai]):
                        self._visit_frame(frames, fi, todo)
        return self

    def _visit_frame(self, frames, fi, todo_groups):
        if fi in self.frames:
            return
        self.frames.add(fi)
        fr = frames[fi]
        if fr is None:
            return
        for key in ('attack', 'attackB', 'editAttack', 'editAttackB'):
            for box in arr_items(fr.props.get(key)):
                if isinstance(box, M.AmfObj) and box.props.get('refName'):
                    self.attacks.add(box.props['refName'])
        for ctx, pt in frame_pts(fr):
            resolution = resolve_pt(self.mod, pt, ctx)
            self.pt_resolutions.append(resolution)
            self.resolution_warnings.extend(resolution.warnings)
            rn = pt.props.get('refName')
            ref = ival(pt.props.get('ref'))
            if rn:
                self.pt_names.add(rn)
                if ctx == 'cre':
                    self.pt_cre_names.add(rn)
            if ref >= 0:
                self.pt_indexes.add((ref, ctx))
            if ctx == 'cre':
                # Item groups are selected from the post-template Pt, not the
                # raw cre.int1. This is what keeps sinan/lucas/leo/saws aligned
                # with the game's CheckCopyRefPt path.
                i1 = resolution.final_int1
                if i1 >= 0:
                    todo_groups.add(i1)
        sfx = ival(fr.props.get('sfx'))
        if sfx >= 0:
            self.sfx.add(sfx)
        for z in arr_items(fr.props.get('uz')) + arr_items(fr.props.get('lz')):
            if isinstance(z, M.AmfObj):
                i = ival(z.props.get('i'))
                if i >= 0:
                    self.limb_slots.add(i)
        for key in ('upLimbs', 'lowLimbs'):
            for value in arr_items(fr.props.get(key)):
                i = ival(value)
                if i >= 0:
                    self.limb_slots.add(i)
        # 渲染引用帧（可能位于其他动作的帧段之外）
        for key in ('refIndex', 'refIndex_2'):
            r = ival(fr.props.get(key), 0)
            if r > 0 and r < len(frames) and r not in self.frames:
                self._visit_frame(frames, r, todo_groups)


def spt_limb_names(spt_obj):
    out = set()
    for sl in arr_items(spt_obj.props.get('spriteLimb')):
        if not isinstance(sl, M.AmfObj):
            continue
        nm = sl.props.get('limbName')
        if nm:
            out.add(nm)
        lns = sl.props.get('limbNames')
        if isinstance(lns, (M.AmfEcmaArray, M.AmfObj)):
            for v in lns.props.values():
                if isinstance(v, str) and v:
                    out.add(v)
    return out


def spt_refs(spt_obj, mod=None):
    """角色 SPT 的直接外部引用。"""
    refs = {'attacks': set(), 'pt_names': set(), 'pt_cre_names': set(),
            'pt_indexes': set(), 'int1': set(), 'sfx': set(),
            'pt_resolutions': [], 'resolution_warnings': []}
    for fi, fr in enumerate(arr_items(spt_obj.props.get('frame'))):
        if not isinstance(fr, M.AmfObj):
            continue
        for key in ('attack', 'attackB', 'editAttack', 'editAttackB'):
            for box in arr_items(fr.props.get(key)):
                if isinstance(box, M.AmfObj) and box.props.get('refName'):
                    refs['attacks'].add(box.props['refName'])
        for ctx, pt in frame_pts(fr):
            resolution = resolve_pt(mod, pt, '%s:%d' % (ctx, fi)) if mod else None
            if resolution is not None:
                refs['pt_resolutions'].append(resolution)
                refs['resolution_warnings'].extend(resolution.warnings)
            rn = pt.props.get('refName')
            ref = ival(pt.props.get('ref'))
            if rn:
                refs['pt_names'].add(rn)
                if ctx == 'cre':
                    refs['pt_cre_names'].add(rn)
            if ref >= 0:
                refs['pt_indexes'].add((ref, ctx))
            if ctx == 'cre':
                i1 = resolution.final_int1 if resolution else ival(pt.props.get('int1'), -1)
                if i1 >= 0:
                    refs['int1'].add(i1)
        sfx = ival(fr.props.get('sfx'))
        if sfx >= 0:
            refs['sfx'].add(sfx)
    for key in ('sfxHurt', 'guardSfx'):
        v = ival(spt_obj.props.get(key))
        if v >= 0:
            refs['sfx'].add(v)
    return refs


def resolve_pt_indexes(mod, pt_indexes):
    """(index, ctx) -> 名字集合 + cre 上下文名字集合；无名模板报错清单。"""
    names, cre_names, bad = set(), set(), []
    for ref, ctx in pt_indexes:
        if 0 <= ref < len(mod.gdat.pts):
            nm = mod.gdat.pts[ref].props.get('name')
            if nm:
                names.add(nm)
                if ctx == 'cre':
                    cre_names.add(nm)
            else:
                bad.append((ref, ctx, 'unnamed'))
        else:
            bad.append((ref, ctx, 'out-of-range'))
    return names, cre_names, bad


def expand_pt_chains(mod, names, cre_names):
    """pt 模板链（ref/refName）+ 模板 int1（cre 上下文）收集。"""
    todo = list(names)
    seen = set()
    cre_ctx = set(cre_names)
    extra_int1 = set()
    while todo:
        nm = todo.pop()
        if nm in seen:
            continue
        seen.add(nm)
        ent = mod.pt_by_name.get(nm)
        if not ent:
            continue
        p = ent[1]
        if nm in cre_ctx:
            i1 = ival(p.props.get('int1'), -1)
            if i1 >= 0:
                extra_int1.add(i1)
        rn = p.props.get('refName')
        ref = ival(p.props.get('ref'))
        nxt = None
        if rn:
            nxt = rn
        elif 0 <= ref < len(mod.gdat.pts):
            nxt = mod.gdat.pts[ref].props.get('name')
        if nxt:
            todo.append(nxt)
            if nm in cre_ctx:
                cre_ctx.add(nxt)
    return seen, cre_ctx, extra_int1


def expand_attack_chains(mod, names):
    todo = list(names)
    seen = set()
    while todo:
        nm = todo.pop()
        if nm in seen:
            continue
        seen.add(nm)
        ent = mod.attack_by_name.get(nm)
        if not ent:
            continue
        a = ent[1]
        rn = a.props.get('refName')
        ref = ival(a.props.get('ref'))
        if rn:
            todo.append(rn)
        elif 0 <= ref < len(mod.gdat.attacks):
            nm2 = mod.gdat.attacks[ref].props.get('name')
            if nm2:
                todo.append(nm2)
    return seen
