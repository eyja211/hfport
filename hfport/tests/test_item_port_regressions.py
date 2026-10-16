# -*- coding: utf-8 -*-
"""HF-EX -> HF_Eyja_0.1.8 的 item 移植回归。

这些用例针对的是用户实测报告的三个症状，断言写成"机制 + 修复"两段：先证明目标
的 Global.pt 真的会劫持源 cre（红），再证明输出已经不可能被劫持（绿）。数字都是
从两个 EXE 实测出来的，不是推理出来的。

  症状一  移植的 sinan 的 38 号 item(sinanmpexp)、lucas/leo 的 5 号 item(lucasB)
          用的还是 Eyja 自己的 —— 两版同名同下标，旧流程一个组都没复制。
  症状二  saws 两个技能的 item 互换、lucas2(Azrael) 的 item 全变 thunder_big_ball
          —— 源 cre 的 ref 被保留，目标 Pt 模板整片覆盖了 int1。
  症状三  item 贴图全塞进 397lmi —— 现在装进独立 LMI，目标 itemLmi 字节不变。
"""
import os
import unittest

from hfport import amf as M
from hfport import gamedata as G
from hfport import porter as P
from hfport import swf as S

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SRC = os.path.join(BASE, 'HF-EX V 0.2.5.exe')
DST = os.path.join(BASE, 'HF_Eyja_0.1.8_ +port22.exe')
CHARS = ['sinan', 'lucas', 'leo', 'saws', 'lucas2']

_CACHE = {}


def _load(path, name):
    if path not in _CACHE:
        _, swf_bytes = S.read_projector(path)
        view = G.ModView(S.SwfFile.parse(swf_bytes), name)
        view.load_all_lmis()
        _CACHE[path] = view
    return _CACHE[path]


def _group_names(spt):
    return [g.props.get('name') if isinstance(g, M.AmfObj) else None
            for g in G.arr_items(spt.props.get('actionGroup'))]


def _cre_rows(view_spt):
    out = []
    for fi, fr in enumerate(G.arr_items(view_spt.props.get('frame'))):
        if not isinstance(fr, M.AmfObj):
            continue
        for ctx, pt in G.frame_pts(fr):
            if ctx == 'cre':
                out.append((fi, G.ival(pt.props.get('ref'), -1),
                            G.ival(pt.props.get('int1'), -1),
                            pt.props.get('refName')))
    return out


def _chain(limb_view, item_spt, group_index):
    """把一个 item 组解析成"运行时真正会用到什么"。

    组 -> actionIndex -> action -> frame 链 -> frame.uz/lz(LZ{i,p})
      -> spriteLimb[i].limbName -> Global.limb[name].limbPicIndex[p]
      -> limbPic.filename
    frame_sig 刻意丢掉 index/refIndex/refIndex_2/uz/lz/upLimbs/lowLimbs：这些是
    重编号会变的字段，比较它们只会证明"下标不同"，而不是"内容不同"。
    """
    groups = G.arr_items(item_spt.props['actionGroup'])
    actions = G.arr_items(item_spt.props['action'])
    frames = G.arr_items(item_spt.props['frame'])
    slots = G.arr_items(item_spt.props['spriteLimb'])
    group = groups[group_index]
    acts = []
    for cat in G.arr_items(group.props.get('actionIndex')):
        for value in G.arr_items(cat):
            ai = G.ival(value, -1)
            if 0 <= ai < len(actions) and actions[ai] is not None and ai not in acts:
                acts.append(ai)
    frs, seen = [], set()
    for ai in acts:
        for fi in G.frames_of_action(frames, actions[ai]):
            if fi not in seen:
                seen.add(fi)
                frs.append(fi)
    limb_names, filenames = [], []
    for fi in frs:
        fr = frames[fi]
        for key in ('uz', 'lz'):
            for z in G.arr_items(fr.props.get(key)):
                if not isinstance(z, M.AmfObj):
                    continue
                si = G.ival(z.props.get('i'), -1)
                pi = G.ival(z.props.get('p'), -1)
                name = None
                if 0 <= si < len(slots) and isinstance(slots[si], M.AmfObj):
                    name = slots[si].props.get('limbName')
                limb_names.append(name)
                filename = None
                if name and name in limb_view.limbs:
                    _, limb, lmi = limb_view.limbs[name]
                    idxs = G.arr_items(limb.props.get('limbPicIndex'))
                    if 0 <= pi < len(idxs):
                        k = G.ival(idxs[pi], -1)
                        if 0 <= k < len(lmi.limb_pics):
                            filename = lmi.limb_pics[k].props.get('filename')
                filenames.append(filename)
    drop = ('index', 'refIndex', 'refIndex_2', 'uz', 'lz',
            'upLimbs', 'lowLimbs', 'ref', 'ref_2')
    sig = G.md5(b'|'.join(G.norm_obj_bytes(frames[f], drop=drop) for f in frs))
    return {'actions': acts, 'frames': frs, 'frame_sig': sig,
            'limb_names': limb_names, 'filenames': filenames}


@unittest.skipUnless(os.path.exists(SRC) and os.path.exists(DST),
                     'HF-EX / HF_Eyja_0.1.8 +port22 EXE 不在工作目录')
class ItemPortRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _load(SRC, 'src')
        cls.dst = _load(DST, 'dst')
        cls.porter = P.Porter(cls.src, cls.dst,
                              P.Plan(CHARS, namespace='hfex'),
                              log=lambda *a: None)
        cls.porter.analyze()
        cls.porter.transform()
        cls.new_p, cls.rep_p, cls.manifest = cls.porter.payloads()
        cls.out_names = _group_names(cls.porter.out_item)
        cls.src_names = _group_names(cls.src.item)
        cls.dst_names = _group_names(cls.dst.item)
        # 输出侧的 limb 查表 = 目标原有 limb + 本次新装的 LMI 里的 limb。
        # 运行时 Global.limb 是全局按名字的一张表，这里必须一样。
        merged = dict(cls.dst.limbs)
        out_files = [(cls.porter.item_lmi_path, cls.porter.out_itemlmi)]
        out_files += [(p, cls.porter.out_lmis[p]) for p in cls.porter.out_lmi_order]
        for path, lmi in out_files:
            if lmi is None:
                continue
            for limb in lmi.limbs:
                nm = limb.props.get('name')
                if nm:
                    merged[nm] = (path, limb, lmi)
        cls.out_view = type('OutView', (), {'limbs': merged})()

    # ---------------- 机制（红）：不修就一定错 ----------------

    def test_source_has_no_pt_templates_but_target_has_many(self):
        """整条 bug 链的前提：源里 ref 是死的，目标里 ref 是活的。"""
        self.assertEqual(len(self.src.gdat.pts), 0)
        self.assertGreater(len(self.dst.gdat.pts), 200)

    def test_target_pt_table_would_swap_saws_two_skills(self):
        rows = _cre_rows(self.src.spt('saws'))
        by_ref = {(ref, int1) for _, ref, int1, _ in rows}
        self.assertIn((142, 38), by_ref)
        self.assertIn((143, 78), by_ref)
        dpts = self.dst.gdat.pts
        # 目标模板会把 38 覆盖成 78、把 78 覆盖成 38 —— 正好互换。
        self.assertEqual(G.ival(dpts[142].props.get('int1')), 78)
        self.assertEqual(G.ival(dpts[143].props.get('int1')), 38)
        self.assertEqual(self.src_names[38], 'sinanmpexp')
        self.assertEqual(self.src_names[78], 'sawsground')

    def test_target_pt_table_would_turn_azrael_into_thunder_big_ball(self):
        rows = _cre_rows(self.src.spt('lucas2'))
        refs = sorted({ref for _, ref, int1, _ in rows if int1 == 80})
        self.assertEqual(refs, [147, 149])
        self.assertEqual(self.src_names[80], 'azraellaser')
        dpts = self.dst.gdat.pts
        for ref in refs:
            hijacked = G.ival(dpts[ref].props.get('int1'))
            self.assertEqual(self.dst_names[hijacked], 'thunder_big_ball')

    def test_same_named_group_is_not_equivalent_across_builds(self):
        """症状一之所以静默：名字、下标、连 ActionGroup 记录本身都一样。

        ActionGroup 只存 actionIndex 数字和 item 元数据，两版正好相同，所以组级
        比较证明不了任何事 —— 差异全在下游的 action/frame/贴图链上。
        """
        for index, name in ((5, 'lucasB'), (38, 'sinanmpexp')):
            self.assertEqual(self.src_names[index], name)
            self.assertEqual(self.dst_names[index], name)
            src_chain = _chain(self.src, self.src.item, index)
            dst_chain = _chain(self.dst, self.dst.item, index)
            self.assertEqual(src_chain['actions'], dst_chain['actions'])
            self.assertEqual(src_chain['frames'], dst_chain['frames'])
            self.assertNotEqual(
                src_chain['frame_sig'], dst_chain['frame_sig'],
                '组 %d(%s) 两版帧内容一样的话就没什么可移植的了' % (index, name),
            )

    def test_same_texture_filename_holds_different_bytes_across_builds(self):
        """lucasB 的贴图必须改名，否则全局 filename 表里目标那份会赢。"""
        src_chain = _chain(self.src, self.src.item, 5)
        shared = {fn for fn in src_chain['filenames'] if fn}
        self.assertTrue(shared)
        differing = {fn for fn in shared
                     if self.src.pngs.get(fn) and self.dst.pngs.get(fn)
                     and self.src.pngs[fn][1] != self.dst.pngs[fn][1]}
        self.assertTrue(differing, '两版同名贴图字节相同，这条回归就失效了')
        for fn in differing:
            self.assertIn(fn, self.porter.png_map)
            self.assertNotEqual(self.porter.png_map[fn], fn,
                                '贴图 %s 没改名，会被目标那份顶掉' % fn)

    def test_ported_group_reproduces_the_source_frame_chain(self):
        """输出里的 lucasB / sinanmpexp 必须是 HF-EX 的帧内容，不是目标同名组的。"""
        for src_index, name in ((5, 'lucasB'), (38, 'sinanmpexp')):
            self.assertIn(src_index, self.porter.group_map)
            out_index = self.porter.group_map[src_index]
            self.assertEqual(self.out_names[out_index], name)
            src_chain = _chain(self.src, self.src.item, src_index)
            out_chain = _chain(self.out_view, self.porter.out_item, out_index)
            dst_chain = _chain(self.dst, self.dst.item, src_index)
            self.assertEqual(len(src_chain['frames']), len(out_chain['frames']))
            self.assertEqual(src_chain['frame_sig'], out_chain['frame_sig'],
                             '组 %s 的帧内容不是源版本的' % name)
            self.assertNotEqual(dst_chain['frame_sig'], out_chain['frame_sig'],
                                '组 %s 复用了目标同名组的内容' % name)
            # 独立包里组名与源版本一致（不进 iteHash，不会顶掉目标同名组）
            self.assertEqual(self.out_names[out_index], self.src_names[src_index])
            # 贴图引用必须逐条按 png_map / limb_map 对上源版本
            mapped = [self.porter.png_map.get(fn, fn) if fn else fn
                      for fn in src_chain['filenames']]
            self.assertEqual(mapped, out_chain['filenames'])
            self.assertEqual(
                [self.porter.limb_map.get(n, n) for n in src_chain['limb_names']],
                out_chain['limb_names'],
            )

    # ---------------- 修复（绿） ----------------

    def test_every_ported_cre_is_detemplated(self):
        """ref 必须全部为 -1，否则目标 Pt 表还能覆盖 int1。"""
        for cid, spt in self.porter.out_spts.items():
            for fi, ref, int1, refname in _cre_rows(spt):
                self.assertEqual(
                    ref, -1,
                    '%s 帧 %d 的 cre 还带着 ref=%d' % (cid, fi, ref))

    def test_saws_two_skills_point_at_the_source_groups_not_swapped(self):
        got = {self.out_names[int1]
               for _, _, int1, _ in _cre_rows(self.porter.out_spts['saws'])
               if int1 >= 0}
        # 独立包里组名保持源版本原样（进不了 iteHash，不会顶掉目标同名组）
        self.assertIn('sinanmpexp', got)
        self.assertIn('sawsground', got)
        # 而且指向的是独立包的局部下标，不是目标 itemSpt
        for _, _, int1, _ in _cre_rows(self.porter.out_spts['saws']):
            if int1 >= 0:
                self.assertLess(int1, len(self.out_names))

    def test_azrael_items_stay_azrael(self):
        got = {self.out_names[int1]
               for _, _, int1, _ in _cre_rows(self.porter.out_spts['lucas2'])
               if int1 >= 0}
        self.assertTrue(any(n.startswith('azrael') for n in got), got)
        self.assertNotIn('thunder_big_ball', got)

    def test_every_migrated_cre_carries_the_route(self):
        """没有 itemSptRoute 的 cre 会退回目标默认包，静默用错道具。"""
        for cid, spt in self.porter.out_spts.items():
            for fi, fr in enumerate(G.arr_items(spt.props.get('frame'))):
                if not isinstance(fr, M.AmfObj):
                    continue
                for ctx, pt in G.frame_pts(fr):
                    if ctx != 'cre':
                        continue
                    if G.ival(pt.props.get('int1'), -1) >= 0:
                        self.assertEqual(pt.props.get('itemSptRoute'), 'hfex',
                                         '%s 帧 %d 的 cre 缺少路由' % (cid, fi))

    def test_routed_item_pack_is_self_contained(self):
        """独立包内部的下标必须全部指向包内，不能越界到目标表。"""
        item = self.porter.out_item
        groups = G.arr_items(item.props['actionGroup'])
        actions = G.arr_items(item.props['action'])
        frames = G.arr_items(item.props['frame'])
        slots = G.arr_items(item.props['spriteLimb'])
        self.assertEqual(item.props.get('id'), 'item_hfex')
        for gi, g in enumerate(groups):
            self.assertEqual(G.ival(g.props.get('index'), -1), gi)
            for cat in G.arr_items(g.props.get('actionIndex')):
                for v in G.arr_items(cat):
                    ai = G.ival(v, -1)
                    if ai >= 0:
                        self.assertLess(ai, len(actions))
        for a in actions:
            fi = G.ival(a.props.get('frameIndex'), -1)
            if fi >= 0:
                self.assertLess(fi, len(frames))
        for fr in frames:
            for key in ('refIndex', 'refIndex_2'):
                r = G.ival(fr.props.get(key), 0)
                if r > 0:
                    self.assertLess(r, len(frames))
            for key in ('uz', 'lz'):
                for z in G.arr_items(fr.props.get(key)):
                    if isinstance(z, M.AmfObj):
                        i = G.ival(z.props.get('i'), -1)
                        if i >= 0:
                            self.assertLess(i, len(slots))
            # 独立包内部的 cre 也必须带路由，否则子道具会跑到目标包里
            for ctx, pt in G.frame_pts(fr):
                if ctx == 'cre' and G.ival(pt.props.get('int1'), -1) >= 0:
                    self.assertEqual(pt.props.get('itemSptRoute'), 'hfex')

    def test_copied_group_content_comes_from_the_source(self):
        """sinanmpexp / lucasB 必须是 HF-EX 的内容，不是同名的目标内容。"""
        src_groups = G.arr_items(self.src.item.props['actionGroup'])
        out_groups = G.arr_items(self.porter.out_item.props['actionGroup'])
        for src_index, name in ((5, 'lucasB'), (38, 'sinanmpexp')):
            self.assertIn(src_index, self.porter.group_map,
                          '组 %d(%s) 没进闭包' % (src_index, name))
            out_index = self.porter.group_map[src_index]
            self.assertEqual(self.out_names[out_index], name)
            drop = ('index', 'name', 'actionIndex', 'keyTriggerRefIndex',
                    'ite_atkName1', 'ite_atkName2', 'piece')
            self.assertEqual(
                G.norm_obj_bytes(src_groups[src_index], drop=drop),
                G.norm_obj_bytes(out_groups[out_index], drop=drop),
            )

    def test_transitive_item_group_is_collected_and_remapped(self):
        """sinanmpexp(38) 不是 sinan 直接 cre 的组，是 sinanmp(37) 的爆炸产物。

        它只能靠闭包的 组 -> 帧 -> cre -> 组 递归收进来，而这正是旧代码坏掉的那一
        环。所以这里断言的是 37 的输出组里真的有一条 cre 指向 38 的输出组。
        """
        direct = {int1 for _, _, int1, _ in _cre_rows(self.porter.out_spts['sinan'])
                  if int1 >= 0}
        self.assertIn(37, self.porter.group_map)
        self.assertIn(38, self.porter.group_map)
        out37 = self.porter.group_map[37]
        out38 = self.porter.group_map[38]
        self.assertIn(out37, direct, 'sinan 应当直接生成 sinanmp')
        self.assertNotIn(out38, direct, 'sinanmpexp 不该是 sinan 的直接 cre')
        self.assertEqual(self.out_names[out37], 'sinanmp')
        self.assertEqual(self.out_names[out38], 'sinanmpexp')

        spawned = set()
        chain = _chain(self.out_view, self.porter.out_item, out37)
        frames = G.arr_items(self.porter.out_item.props['frame'])
        for fi in chain['frames']:
            for ctx, pt in G.frame_pts(frames[fi]):
                if ctx == 'cre':
                    self.assertEqual(G.ival(pt.props.get('ref'), -1), -1)
                    spawned.add(G.ival(pt.props.get('int1'), -1))
        self.assertIn(out38, spawned,
                      'sinanmp 的帧没有指向移植后的 sinanmpexp')

    def test_routed_group_names_match_the_source_exactly(self):
        """独立包的组名不进 iteHash（那只遍历 sptHash["item"]），所以不必改名。

        改名反而会让独立包和源版本对不上，HFWorkshop 里也认不出来。
        """
        # 整包 1:1 复制：组数与源相同，组号恒等，组名逐个相同。
        self.assertEqual(len(self.out_names), len(self.src_names))
        self.assertEqual(self.out_names, self.src_names)
        for src_i, out_i in self.porter.group_map.items():
            self.assertEqual(src_i, out_i, '组号必须是恒等映射')

    def test_target_item_spt_is_untouched(self):
        self.assertNotIn('Data.Global_itemSpt', self.rep_p)
        self.assertNotIn('Data.Global_itemSpt', self.new_p)
        self.assertEqual(
            G.sha256(self.dst.swf.binary_payload('Data.Global_itemSpt')),
            self.manifest['target_item_hashes']['spt_payload_sha256'])

    # ---------------- 症状三 + 加载时序 ----------------

    def test_target_item_lmi_payload_is_untouched(self):
        self.assertNotIn('Data.Global_itemLmi', self.new_p)
        self.assertNotIn('Data.Global_itemLmi', self.rep_p)
        self.assertEqual(
            G.sha256(self.dst.swf.binary_payload('Data.Global_itemLmi')),
            self.manifest['target_item_hashes']['lmi_payload_sha256'],
        )

    def test_item_textures_go_into_their_own_lmi(self):
        self.assertIsNotNone(self.porter.out_itemlmi)
        cls = 'Data.Global_hfex_itemLmi'
        self.assertIn(cls, self.new_p)
        self.assertGreater(len(self.porter.out_itemlmi.limb_pics), 0)

    def test_item_lmi_loads_before_its_own_item_spt(self):
        """limbName 只在 SPT 解析那一刻解析一次（Spt.as:1133）。

        独立 item SPT 的 spriteLimb 指向独立 LMI 里的 limb，所以 LMI 的加载调用必须
        排在 SPT 之前；顺序反了会一张贴图都不画，而且不报任何错。
        """
        res = self.manifest['item_lmi_resource']
        paths = [p for _, _, p in self.manifest['load_sequence']]
        self.assertIn(res['lmi_path'], paths)
        self.assertIn(res['spt_path'], paths)
        self.assertLess(paths.index(res['lmi_path']), paths.index(res['spt_path']))
        self.assertEqual(res['spt_id'], 'item_hfex')
        self.assertIn('Data.Global_hfex_itemSpt', self.new_p)

    def test_item_limb_is_renamed_so_the_target_limb_survives(self):
        for name, mapped in self.manifest['item_limb_map'].items():
            self.assertNotEqual(name, mapped)
            self.assertNotIn(mapped, self.dst.limbs)

    def test_piece_actions_map_by_name_into_the_target_global_spt(self):
        dst_actions = G.arr_items(self.dst.spt('global').props.get('action'))
        src_actions = G.arr_items(self.src.spt('global').props.get('action'))
        for src_i, dst_i in self.manifest['global_action_map'].items():
            src_i = int(src_i)
            self.assertEqual(src_actions[src_i].props.get('name'),
                             dst_actions[dst_i].props.get('name'))
        for group in G.arr_items(self.porter.out_item.props['actionGroup'])[
                len(self.dst_names):]:
            for piece in G.arr_items(group.props.get('piece')):
                if isinstance(piece, M.AmfObj):
                    self.assertLess(G.ival(piece.props.get('i'), -1),
                                    len(dst_actions))


    def test_frame_indices_stay_1to1_with_the_source(self):
        """帧/动作/组下标必须与源恒等 —— 这是 ball 那个 bug 的教训。

        `ObjI::Run` 会在动作帧之外按 fi 偏移去取旋转/斜向变体（`ai == C._45 ||
        ai == C._90` 那条分支），所以"某动作 last 帧之后紧跟哪一帧"是引擎依赖的数据。
        原来做密排重编号，easonball 飞行动作 last 帧（源 754）后面本该还是 6 帧
        ball1/ball2 变体（源 755-760），密排后被换成了爆炸帧 —— 实测表现是飞行贴图
        和爆炸贴图不断交替。
        """
        s_frames = G.arr_items(self.src.item.props['frame'])
        o_frames = G.arr_items(self.porter.out_item.props['frame'])
        s_acts = G.arr_items(self.src.item.props['action'])
        o_acts = G.arr_items(self.porter.out_item.props['action'])
        self.assertEqual(len(o_frames), len(s_frames))
        self.assertEqual(len(o_acts), len(s_acts))
        for i, (sa, oa) in enumerate(zip(s_acts, o_acts)):
            if isinstance(sa, M.AmfObj) and isinstance(oa, M.AmfObj):
                self.assertEqual(G.ival(oa.props.get('frameIndex'), -1),
                                 G.ival(sa.props.get('frameIndex'), -1),
                                 'action %d 的 frameIndex 变了' % i)
        for i, (sf, of) in enumerate(zip(s_frames, o_frames)):
            if not (isinstance(sf, M.AmfObj) and isinstance(of, M.AmfObj)):
                continue
            self.assertEqual(bool(of.props.get('last')), bool(sf.props.get('last')))
            for key in ('refIndex', 'refIndex_2'):
                self.assertEqual(G.ival(of.props.get(key), 0),
                                 G.ival(sf.props.get(key), 0),
                                 '帧 %d 的 %s 变了' % (i, key))

    def test_the_frame_after_a_fly_action_still_carries_fly_art(self):
        """具体钉住 easonball：越过 last 之后相邻几帧仍是 ball1/ball2，不是爆炸帧。"""
        s_frames = G.arr_items(self.src.item.props['frame'])
        o_frames = G.arr_items(self.porter.out_item.props['frame'])
        _, s_limb, s_lf = self.src.limbs['item']
        s_idx = G.arr_items(s_limb.props.get('limbPicIndex'))
        merged = self.out_view.limbs
        _, o_limb, o_lf = merged['item@hfex']
        o_idx = G.arr_items(o_limb.props.get('limbPicIndex'))

        def slots(frames, fi):
            fr = frames[fi]
            uz = G.arr_items(fr.props.get('uz'))
            if not uz:
                r = G.ival(fr.props.get('refIndex'), 0)
                if 0 < r < len(frames):
                    uz = G.arr_items(frames[r].props.get('uz'))
            return tuple(G.ival(z.props.get('p'), -1) for z in uz
                         if isinstance(z, M.AmfObj))

        def fname(idx, lf, p):
            pi = G.ival(idx[p], -1) if 0 <= p < len(idx) else -1
            return (lf.limb_pics[pi].props.get('filename')
                    if 0 <= pi < len(lf.limb_pics) else None)

        # 源帧 753/754 = 飞行动作；755-760 是它后面的 ball1/ball2 变体。
        for fi in range(753, 761):
            sp = slots(s_frames, fi)
            op = slots(o_frames, fi)
            self.assertEqual(op, sp, '帧 %d 的贴图槽位与源不同' % fi)
            for p in sp:
                sf = fname(s_idx, s_lf, p)
                of = fname(o_idx, o_lf, p)
                self.assertTrue(sf and of, '帧 %d 槽位 %d 解析不到贴图' % (fi, p))
                self.assertEqual(of, self.porter.png_map.get(sf, sf))
                self.assertNotIn('explode', sf,
                                 '帧 %d 在源里就该是飞行贴图' % fi)

if __name__ == '__main__':
    unittest.main()