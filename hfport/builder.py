# -*- coding: utf-8 -*-
"""总装配：数据合并 + 注册代码补丁 + SWF/EXE 输出。

四个补丁点（全部“尾部追加”，不改动既有指令流）：
  A Global.LoadGlobalDatFromCompressedBytes  -> pow/charListOrder/langWords/Console.sfx 注册
  B Data.BgInfoFile.LoadFromCompressedBytes  -> 新 LMI/SPT 的加载调用（按 refSpt 拓扑序）
  C Global.InitPow                            -> 经验绑定 pow[new].c = pow[target].c（带空值守卫）
  D Web_misc.SelectCharPanel 的 cinit          -> charOption 追加（类内私有访问，无需改可见性）
"""
import json, os, struct

from . import abcpatch as A
from . import amf as M
from . import gamedata as G
from . import porter as P
from . import registry as R
from . import swf as S


class Builder:
    def __init__(self, src_exe, dst_exe, out_exe, chars, work_dir,
                 ffdec_jar=None, display_suffix=None, namespace=None, log=print):
        self.src_exe = src_exe
        self.dst_exe = dst_exe
        self.out_exe = out_exe
        self.chars = chars
        self.work = work_dir
        self.ffdec = ffdec_jar
        self.display_suffix = display_suffix
        self.requested_namespace = namespace
        self.log = log

    # ------------------------------------------------------------ 主流程

    def run(self):
        log = self.log
        src_abs = os.path.normcase(os.path.abspath(self.src_exe))
        dst_abs = os.path.normcase(os.path.abspath(self.dst_exe))
        out_abs = os.path.normcase(os.path.abspath(self.out_exe))
        if out_abs in (src_abs, dst_abs):
            raise RuntimeError('输出 EXE 不能覆盖源版本或目标版本')
        if not self.chars:
            raise RuntimeError('至少选择一个角色')
        if len(set(self.chars)) != len(self.chars):
            raise RuntimeError('角色选择中存在重复 ID')
        log('[1/8] 读取两个版本...')
        self.dst_prefix, dst_swf = S.read_projector(self.dst_exe)
        _, src_swf = S.read_projector(self.src_exe)
        self.dst = G.ModView(S.SwfFile.parse(dst_swf), 'dst')
        self.src = G.ModView(S.SwfFile.parse(src_swf), 'src')
        log('   源肢体 %d，目标肢体 %d' % (0, 0))
        self.src.load_all_lmis()
        self.dst.load_all_lmis()
        log('   源肢体 %d，目标肢体 %d' % (len(self.src.limbs), len(self.dst.limbs)))

        log('[2/8] 提取版本清单与预检...')
        self._extract_registry()

        log('[3/8] 计算依赖闭包与冲突决策...')
        porter = P.Porter(self.src, self.dst, self.plan, log=log)
        porter.analyze()
        log('[4/8] 数据变换与合并...')
        porter.transform()
        self.new_p, self.rep_p, self.manifest = porter.payloads()
        self.porter = porter
        log('   新增载荷 %d 个，替换载荷 %d 个' % (len(self.new_p), len(self.rep_p)))
        self.manifest['char_meta'] = self.char_meta

        log('[5/8] 生成注册代码补丁...')
        self._patch_abc()

        log('[6/8] 生成新资产类...')
        self._build_assets()

        log('[7/8] 组装 SWF 标签...')
        self._assemble()

        log('[8/8] 写出 EXE...')
        out_swf = self.dst.swf.build()
        S.write_projector(self.out_exe, self.dst_prefix, out_swf)
        log('完成: %s (%d MB)' % (self.out_exe, os.path.getsize(self.out_exe) // 1048576))

        report = {
            'manifest': self.manifest,
            'chars': self.char_meta,
            'new_class_ids': self.new_class_ids,
            'out_exe': self.out_exe,
            'namespace': self.plan.namespace,
            'src_hash': self.src_hash,
            'dst_hash': self.dst_hash,
            'asset_abc_name': self.asset_abc_name,
            'charlist_pages': self.charlist_page_info,
        }
        rp = os.path.join(self.work, 'build_report.json')
        json.dump(report, open(rp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=str)
        log('构建报告: %s' % rp)
        return report

    # ------------------------------------------------------------ 注册数据

    def _extract_registry(self):
        self.src_hash = R.export_cache_key(self.src_exe)
        self.dst_hash = R.export_cache_key(self.dst_exe)
        self.src_inventory = R.load_inventory(self.work, self.ffdec, self.src_exe)
        self.dst_inventory = R.load_inventory(self.work, self.ffdec, self.dst_exe)
        sinv = self.src_inventory
        dinv = self.dst_inventory
        self.src.sfx_class_by_index = dict(sinv.sound_classes)
        self.dst.sfx_class_by_index = dict(dinv.sound_classes)

        src_classes = set(self.src.swf.class_to_char_id())
        missing = []
        for slot in self.chars:
            if slot not in sinv.pow:
                missing.append('pow["%s"]' % slot)
            if slot not in sinv.binding_codes:
                missing.append('经验绑定 "%s"' % slot)
            if 'Data.Global_%sSpt' % slot not in src_classes:
                missing.append('Data.Global_%sSpt' % slot)
        if missing:
            raise RuntimeError('源版本预检失败，缺少: ' + '、'.join(missing))

        source_spt_ids = [
            cls[len('Data.Global_'):-3]
            for cls in src_classes
            if cls.startswith('Data.Global_') and cls.endswith('Spt')
        ]
        occupied_ids = set(dinv.pow)
        occupied_ids.update(cid for _, _, cid in dinv.char_options)
        occupied_names = set(self.dst.limbs)
        occupied_names.update(self.dst.pngs)
        occupied_names.update(self.dst.attack_by_name)
        occupied_names.update(self.dst.pt_by_name)
        for group in G.arr_items(self.dst.item.props.get('actionGroup')):
            if isinstance(group, M.AmfObj) and isinstance(group.props.get('name'), str):
                occupied_names.add(group.props['name'])
        base_ns = self.requested_namespace or P.derive_namespace(self.src_exe)
        namespace = P.allocate_namespace(
            base_ns,
            selected_ids=source_spt_ids,
            occupied_ids=occupied_ids,
            occupied_classes=set(self.dst.swf.class_to_char_id()),
            occupied_names=occupied_names,
        )
        if self.display_suffix is None:
            self.display_suffix = ' (%s)' % namespace.upper()
        self.plan = P.Plan(
            self.chars,
            namespace=namespace,
            display_suffix=self.display_suffix,
        )
        self.log('   命名空间: %s' % namespace)

        bind_map = {}
        for slot in self.chars:
            code = sinv.binding_codes[slot]
            target = R.choose_binding_target(slot, code, dinv.binding_codes)
            if target is None:
                raise RuntimeError(
                    '目标版本没有可承接源角色 "%s" 的合法经验绑定' % slot
                )
            bind_map[slot] = target

        self.base_charlist = list(dinv.char_list_order)

        max_i = 0
        for slot, pairs in dinv.pow.items():
            d = dict(pairs)
            if isinstance(d.get('i'), float):
                max_i = max(max_i, int(d['i']))
        values = [v for v, _, _ in dinv.char_options if v >= 0]
        next_value = (max(values) + 1) if values else 0

        self.char_meta = []
        for k, slot in enumerate(self.chars):
            new_id = self.plan.new_id(slot)
            pow_pairs = [list(x) for x in sinv.pow[slot]]
            for pair in pow_pairs:
                if pair[0] == 'i':
                    pair[1] = float(max_i + 1 + k)
            lw = sinv.langwords.get(slot) or [('en', slot), ('b5', slot)]
            lw2 = [(lang, txt + self.display_suffix) for lang, txt in lw]
            disp_en = dict(lw).get('en', slot) + self.display_suffix
            self.char_meta.append({
                'slot': slot, 'new_id': new_id,
                'pow': pow_pairs,
                'langwords': lw2,
                'bind_to': bind_map.get(slot, 'lucas'),
                'value': next_value + k,
                'display': disp_en,
            })

    # ------------------------------------------------------------ ABC 补丁

    def _find_main_doabc(self):
        for i, t in self.dst.swf.find_tags(S.TAG_DOABC):
            flags, name, abc_bytes = S.parse_doabc(t.body)
            abc = A.AbcFile.parse(abc_bytes)
            if abc.find_instance('Data', 'Global') is not None:
                return i, flags, name, abc
        raise RuntimeError('主 DoABC 未找到')

    def _patch_abc(self):
        tag_i, flags, name, abc = self._find_main_doabc()
        self.abc = abc

        mn_pow = abc.qname_idx(0x16, '', 'pow')
        mn_clo = abc.qname_idx(0x16, '', 'charListOrder')
        mn_lw = abc.qname_idx(0x16, '', 'langWords')
        mn_push = abc.qname_idx(0x16, '', 'push')
        mn_c = abc.qname_idx(0x16, '', 'c')
        mn_sfx = abc.qname_idx(0x16, '', 'sfx')
        mn_console = abc.qname_idx(0x16, '', 'Console')
        mn_gsfx = abc.qname_idx(0x16, 'Game', 'Sfx')
        mn_lif = abc.qname_idx(0x16, 'Data', 'LimbInfoFile')
        mn_spt = abc.qname_idx(0x16, 'Data', 'Spt')
        mn_lfcb = abc.qname_idx(0x16, '', 'LoadFromCompressedBytes')
        mn_ba = abc.qname_idx(0x16, 'flash.utils', 'ByteArray')
        mnl = self._find_public_mnl(abc)

        def push_value(asm, v):
            if isinstance(v, bool):
                (asm.pushtrue if v else asm.pushfalse)()
            elif isinstance(v, (int, float)):
                asm.pushnum(v)
            elif isinstance(v, str):
                asm.pushstring(v)
            else:
                raise TypeError(v)

        # ---- A: 注册（pow/charListOrder/langWords/音效表）----
        asm = A.Asm(abc)
        for meta in self.char_meta:
            asm.getlex(mn_pow)
            asm.pushstring(meta['new_id'])
            for k, v in meta['pow']:
                asm.pushstring(k)
                push_value(asm, v)
            asm.newobject(len(meta['pow']))
            asm.setproperty(mnl, name_on_stack=1)

            asm.getlex(mn_clo)
            asm.pushstring(meta['new_id'])
            asm.callpropvoid(mn_push, 1)

            asm.getlex(mn_lw)
            asm.pushstring(meta['new_id'])
            for lang, txt in meta['langwords']:
                asm.pushstring(lang)
                asm.pushstring(txt)
            asm.newobject(len(meta['langwords']))
            asm.setproperty(mnl, name_on_stack=1)
        for snd in self.manifest['new_sounds']:
            asm.getlex(mn_console)
            asm.getproperty(mn_sfx)
            asm.pushnum(snd['sfx_index'])
            asm._op(0x5D, mn_gsfx, stack_delta=1)       # findpropstrict Game.Sfx
            asm.getlex(abc.qname_idx(0x16, '', snd['class']))
            asm.pushnum(5)
            asm.constructprop(mn_gsfx, 2)
            asm.setproperty(mnl, name_on_stack=1)
        code_a = asm.finish()
        mi, _ = abc.find_class_method('Data', 'Global', 'LoadGlobalDatFromCompressedBytes')
        A.append_tail(abc, abc.body_of_method(mi), code_a, asm.max_stack)
        self.log('   A 注册尾部: %d 字节 (max_stack %d)' % (len(code_a), asm.max_stack))

        # ---- B: 加载调用 ----
        asm = A.Asm(abc)
        for kind, cls, path in self.manifest['load_sequence']:
            short = cls.split('.')[-1]
            asm.getlex(mn_lif if kind == 'lmi' else mn_spt)
            asm.getlex(abc.qname_idx(0x16, 'Data', short))
            asm.construct(0)
            asm.getlex(mn_ba)
            asm._op(0x87, stack_delta=-1)                # astypelate
            asm.pushstring(path)
            asm.callpropvoid(mn_lfcb, 2)
        code_b = asm.finish()
        mi, _ = abc.find_class_method('Data', 'BgInfoFile', 'LoadFromCompressedBytes')
        A.append_tail(abc, abc.body_of_method(mi), code_b, asm.max_stack)
        self.log('   B 加载尾部: %d 字节 (%d 个加载调用)' % (len(code_b), len(self.manifest['load_sequence'])))

        # ---- C: 经验绑定 ----
        mn_calcpow = abc.qname_idx(0x16, '', 'CalcPow')
        asm = A.Asm(abc)
        first_id = self.char_meta[0]['new_id']
        asm.getlex(mn_pow)
        asm.pushstring(first_id)
        asm.getproperty(mnl, name_on_stack=1)
        asm.pushnull()
        asm.ifeq('skip')
        for meta in self.char_meta:
            asm.getlex(mn_pow)
            asm.pushstring(meta['new_id'])
            asm.getproperty(mnl, name_on_stack=1)
            asm.getlex(mn_pow)
            asm.pushstring(meta['bind_to'])
            asm.getproperty(mnl, name_on_stack=1)
            asm.getproperty(mn_c)
            asm.setproperty(mn_c)
            # 绑定后立刻重算等级/经验
            asm._op(0x5D, mn_calcpow, stack_delta=1)   # findpropstrict
            asm.pushstring(meta['new_id'])
            asm.callpropvoid(mn_calcpow, 1)
        asm.label('skip')
        code_c = asm.finish()
        mi, _ = abc.find_class_method('Data', 'Global', 'InitPow')
        A.append_tail(abc, abc.body_of_method(mi), code_c, asm.max_stack)
        self.log('   C 绑定尾部: %d 字节' % len(code_c))

        # ---- D: charOption 追加（cinit 内私有访问）----
        ci = abc.find_instance('Web_misc', 'SelectCharPanel')
        cinit, ctraits = abc.classes[ci]
        mn_charopt = None
        for t in ctraits:
            mn = abc.multinames[t.name]
            if mn and mn[0] in (A.MN_QName, A.MN_QNameA) and abc.strings[mn[1][1]] == 'charOption':
                mn_charopt = t.name
        if mn_charopt is None:
            raise RuntimeError('charOption trait 未找到')
        asm = A.Asm(abc)
        for meta in self.char_meta:
            asm.getlex(mn_charopt)
            asm.pushstring('value')
            asm.pushnum(meta['value'])
            asm.pushstring('name')
            asm.pushstring(meta['display'])
            asm.pushstring('id')
            asm.pushstring(meta['new_id'])
            asm.newobject(3)
            asm.callpropvoid(mn_push, 1)
        code_d = asm.finish()
        A.append_tail(abc, abc.body_of_method(cinit), code_d, asm.max_stack)
        self.log('   D 选人尾部: %d 字节' % len(code_d))

        # ---- E: 角色一览翻页扩容 ----
        self._patch_charlist_pages(abc)

        # 重建 DoABC 标签
        new_abc = abc.build()
        body = struct.pack('<I', flags) + name.encode() + b'\x00' + new_abc
        self.dst.swf.tags[tag_i] = S.Tag(S.TAG_DOABC, body, long_hdr=True)
        self.main_doabc_index = tag_i

    # 角色一览每页格子数（原版布局 5 列 x 3 行）
    CHARLIST_PER_PAGE = 15

    def _patch_charlist_pages(self, abc):
        """把角色一览的翻页上限与页码分母按最终名单人数扩容。

        原版 NextPage 硬卡 pageNum<2、OnEnterFrame 显示 "/2"。移植后一览成员
        = 目标原有 charListOrder + 新角色（新角色已在钩子 A push 进 charListOrder）。
        """
        import math
        base_ids = self.base_charlist  # 目标原有一览人数（不含随机等）
        total = len(base_ids) + len(self.char_meta)
        pages = max(1, math.ceil(total / self.CHARLIST_PER_PAGE))
        mi, _ = abc.find_class_method('Page', 'CharList', 'NextPage', static=False)
        next_body = abc.body_of_method(mi)
        old_pages = self._read_next_page_limit(abc, next_body)
        mi2, _ = abc.find_class_method('Page', 'CharList', 'OnEnterFrame', static=False)
        body2 = abc.body_of_method(mi2)
        denom_idx, denom_pages = self._read_page_denominator(abc, body2)
        current_pages = max(old_pages, denom_pages)
        pages = max(pages, current_pages)
        if pages <= current_pages:
            self.charlist_page_info = {
                'before': current_pages, 'after': current_pages, 'total': total
            }
            self.log('   E 翻页: 人数 %d，当前 %d 页，无需扩容' % (total, current_pages))
            return
        if pages > 127:
            self.warnings_e = '一览页数 %d 超过 127，未扩容' % pages
            self.charlist_page_info = {
                'before': current_pages, 'after': current_pages, 'total': total
            }
            self.log('   E 翻页: ⚠ ' + self.warnings_e)
            return

        # E1: NextPage 中与 pageNum 属性相邻的页数操作数。
        hit = self._patch_next_page_limit(abc, next_body, pages)
        if hit != 1:
            raise RuntimeError('NextPage 语义页数操作数命中 %d 次（预期1）' % hit)

        # E2: 页码分母当前字符串 -> 新建 "/<pages>" 并重指。
        sidx = denom_idx
        uses = A.string_usage_count(abc, sidx)
        new_sidx = abc.str_idx('/%d' % pages)
        patched = self._repoint_pushstring(body2, sidx, new_sidx)
        if patched != 1:
            raise RuntimeError('OnEnterFrame 分母 pushstring 命中 %d 次（预期1）' % patched)
        if uses != 1:
            self.log('   E 翻页: 注意当前分母全局被引用 %d 次，仅改一览分母' % uses)
        self.charlist_page_info = {
            'before': current_pages, 'after': pages, 'total': total
        }
        self.log('   E 翻页: 人数 %d -> %d 页（原 %d 页）' % (total, pages, current_pages))

    @staticmethod
    def _read_next_page_limit(abc, body):
        limits = []
        insns = list(A.walk_code(body.code))
        for i in range(len(insns) - 3):
            a, b, c, d = insns[i:i + 4]
            if (a[1] in (0xD0, 0x62) and b[1] == 0x66 and c[1] == 0x24
                    and 0x0C <= d[1] <= 0x1A):
                limits.append(c[2][0])
        if len(limits) != 1:
            raise RuntimeError('NextPage 当前页数语义模式命中 %d 次' % len(limits))
        return limits[0]

    @staticmethod
    def _patch_next_page_limit(abc, body, new_pages):
        insns = list(A.walk_code(body.code))
        hits = []
        for i in range(len(insns) - 3):
            a, b, c, d = insns[i:i + 4]
            if (a[1] in (0xD0, 0x62) and b[1] == 0x66
                    and c[1] == 0x24 and 0x0C <= d[1] <= 0x1A):
                hits.append(c[0] + 1)
        if len(hits) != 1:
            return len(hits)
        code = bytearray(body.code)
        code[hits[0]] = new_pages
        body.code = bytes(code)
        return 1

    @staticmethod
    def _read_page_denominator(abc, body):
        hits = []
        for off, op, ops, nxt in A.walk_code(body.code):
            if op == 0x2C:
                value = abc.strings[ops[0]]
                if isinstance(value, str) and value.startswith('/') and value[1:].isdigit():
                    hits.append((ops[0], int(value[1:])))
        if len(hits) != 1:
            raise RuntimeError('OnEnterFrame 页码分母字符串命中 %d 次' % len(hits))
        return hits[0]

    @staticmethod
    def _repoint_pushstring(body, old_sidx, new_sidx):
        """把方法体里唯一一条 pushstring old_sidx 改指 new_sidx。

        新字符串索引的 u30 编码长度可能与原来不同，会使后续字节整体平移，
        因此整段重新发射并按"指令起点旧->新偏移"映射修正所有相对跳转。
        返回命中次数（应为 1）。"""
        insns = list(A.walk_code(body.code))
        hits = [k for k, (off, op, ops, nxt) in enumerate(insns)
                if op == 0x2C and ops[0] == old_sidx]
        if len(hits) != 1:
            return len(hits)

        # 重发射；记录每条指令在新流的起点
        new_bytes = bytearray()
        new_start = []
        for off, op, ops, nxt in insns:
            new_start.append(len(new_bytes))
            if op == 0x2C and ops[0] == old_sidx:
                new_bytes.append(0x2C)
                new_bytes += A.write_u30(new_sidx)
            else:
                new_bytes += body.code[off:nxt]
        end_new = len(new_bytes)
        # 旧指令起点 -> 序号（含末尾哨兵）
        start_idx = {ins[0]: i for i, ins in enumerate(insns)}
        start_idx[len(body.code)] = len(insns)
        new_start_ext = new_start + [end_new]

        out = bytearray(new_bytes)
        for i, (off, op, ops, nxt) in enumerate(insns):
            spec = A.OPCODES[op][1]
            if spec and spec[0] == 'switch':
                raise RuntimeError('OnEnterFrame 含 lookupswitch，暂不支持重排')
            if spec and spec[0] == 's24':
                target_old = nxt + ops[0]          # 相对跳转目标（旧偏移）
                if target_old not in start_idx:
                    raise RuntimeError('跳转目标 %d 不在指令边界' % target_old)
                tgt_new = new_start_ext[start_idx[target_old]]
                src_next_new = new_start_ext[i + 1]  # 本指令之后（新偏移）
                out[new_start[i] + 1:new_start[i] + 4] = A.write_s24(tgt_new - src_next_new)
        body.code = bytes(out)
        return 1

    @staticmethod
    def _find_public_mnl(abc):
        for i in range(1, len(abc.multinames)):
            mn = abc.multinames[i]
            if mn and mn[0] == A.MN_MultinameL:
                for nsi in abc.ns_sets[mn[1][0]]:
                    k, nname = abc.namespaces[nsi]
                    if k == 0x16 and abc.strings[nname] == '':
                        return i
        raise RuntimeError('公共 MultinameL 未找到')

    # ------------------------------------------------------------ 资产类

    def _build_assets(self):
        specs = []
        for cls in sorted(self.new_p):
            specs.append(('Data', cls.split('.')[-1], 'mx.core', 'ByteArrayAsset'))
        for snd in self.manifest['new_sounds']:
            specs.append(('', snd['class'], 'mx.core', 'SoundAsset'))
        self.asset_abc = A.build_asset_abc(specs)
        chk = A.AbcFile.parse(self.asset_abc)
        assert chk.build() == self.asset_abc
        self.log('   资产类 %d 个, DoABC %d 字节' % (len(specs), len(self.asset_abc)))

    # ------------------------------------------------------------ 组装

    def _assemble(self):
        swf = self.dst.swf
        # 替换合并载荷
        for cls, pay in self.rep_p.items():
            swf.replace_binary(cls, pay)

        scs = swf.symbol_classes()
        main_sc_idx, _ = max(scs, key=lambda x: len(x[1]))
        nid = swf.max_char_id() + 1
        asset_abc_name = 'hfport_assets_%s' % self.plan.namespace
        new_tags = [S.make_doabc_tag(self.asset_abc, asset_abc_name)]
        self.asset_abc_name = asset_abc_name
        entries = []
        self.new_class_ids = {}
        for cls in sorted(self.new_p):
            new_tags.append(swf.add_binary(nid, self.new_p[cls]))
            entries.append((nid, cls))
            self.new_class_ids[cls] = nid
            nid += 1
        # 新音效：拷源 DefineSound 内容换 id
        for snd in self.manifest['new_sounds']:
            body = None
            for _, t in self.src.swf.find_tags(S.TAG_DEFINESOUND):
                if t.char_id() == snd['src_tag']:
                    body = struct.pack('<H', nid) + t.body[2:]
                    break
            if body is None:
                raise RuntimeError('源音效 tag %d 未找到' % snd['src_tag'])
            new_tags.append(S.Tag(S.TAG_DEFINESOUND, body, long_hdr=True))
            entries.append((nid, snd['class']))
            self.new_class_ids[snd['class']] = nid
            nid += 1
        swf.insert_tags(main_sc_idx, new_tags)
        swf.add_symbolclass_entries(main_sc_idx + len(new_tags), entries)
        self.log('   新标签 %d 个, SymbolClass 追加 %d 条' % (len(new_tags), len(entries)))
