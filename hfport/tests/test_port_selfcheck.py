# -*- coding: utf-8 -*-
"""M2 测试：对单角色 lucas 跑闭包/变换/合并/输出载荷，并做静态自检。"""
import sys, os, json
sys.stdout.reconfigure(encoding='utf-8')
BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, BASE)

from hfport import swf as S
from hfport import amf as M
from hfport import gamedata as G
from hfport import porter as P


def load_mod(path, name):
    _, swf_bytes = S.read_projector(path)
    f = S.SwfFile.parse(swf_bytes)
    mv = G.ModView(f, name)
    mv.load_all_lmis()
    return mv


def selfcheck(new_p, rep_p, manifest, dst, porter):
    """把合并结果放进目标肢体/攻击空间做静态验证。"""
    errs = []
    src = porter.src
    rev_limb = {v: k for k, v in porter.limb_map.items()}
    rev_atk = {v: k for k, v in porter.attack_map.items()}
    rev_pt = {v: k for k, v in porter.pt_map.items()}
    # 1) 所有载荷可解码
    for cls, pay in list(new_p.items()) + list(rep_p.items()):
        try:
            stream = M.unwrap_payload(pay)
            if 'Spt' in cls:
                G.SptFile.parse(stream)
            elif 'Lmi' in cls:
                G.LmiFile.parse(stream)
            elif 'Dat' in cls:
                G.GdatFile.parse(stream)
        except Exception as ex:
            errs.append('载荷解码失败 %s: %s' % (cls, ex))

    # 2) 构造合并后全局肢体空间 = 目标已有 + 新增
    limb_space = set(dst.limbs.keys())
    for cls, pay in new_p.items():
        if cls.endswith('Lmi'):
            f = G.LmiFile.parse(M.unwrap_payload(pay))
            for l in f.limbs:
                limb_space.add(l.props.get('name'))
    if 'Data.Global_itemLmi' in rep_p:
        f = G.LmiFile.parse(M.unwrap_payload(rep_p['Data.Global_itemLmi']))
        for l in f.limbs:
            limb_space.add(l.props.get('name'))

    # 攻击/pt 名字空间
    gd = G.GdatFile.parse(M.unwrap_payload(rep_p['Data.Global_globalDat']))
    atk_names = {a.props.get('name') for a in gd.attacks if a.props.get('name')}
    pt_names = {p.props.get('name') for p in gd.pts if p.props.get('name')}
    item = G.SptFile.parse(M.unwrap_payload(rep_p['Data.Global_itemSpt'])).obj
    n_groups = len(G.arr_items(item.props.get('actionGroup')))
    item_slots = len(G.arr_items(item.props.get('spriteLimb')))

    # 3) 校验每个新角色 SPT（只在"源里能解析、移植后却断裂"时报错；
    #    源里本就悬空的引用属忠实移植，容忍）
    src_limbs = set(src.limbs.keys())
    src_atk = set(src.attack_by_name.keys())
    src_pt = set(src.pt_by_name.keys())
    for cls, pay in new_p.items():
        if not cls.endswith('Spt'):
            continue
        spt = G.SptFile.parse(M.unwrap_payload(pay)).obj
        cid = spt.props.get('id')
        for sl in G.arr_items(spt.props.get('spriteLimb')):
            if not isinstance(sl, M.AmfObj):
                continue
            for nm in [sl.props.get('limbName')] + (
                    list(sl.props.get('limbNames').props.values())
                    if isinstance(sl.props.get('limbNames'), (M.AmfEcmaArray, M.AmfObj)) else []):
                if not isinstance(nm, str) or not nm:
                    continue
                src_nm = rev_limb.get(nm, nm)
                if src_nm in src_limbs and nm not in limb_space:
                    errs.append('%s: 肢体 %r(源%r) 源可解析但移植后丢失' % (cid, nm, src_nm))
        for fr in G.arr_items(spt.props.get('frame')):
            if not isinstance(fr, M.AmfObj):
                continue
            for key in ('attack', 'attackB', 'editAttack', 'editAttackB'):
                for box in G.arr_items(fr.props.get(key)):
                    if isinstance(box, M.AmfObj):
                        rn = box.props.get('refName')
                        src_rn = rev_atk.get(rn, rn)
                        if rn and src_rn in src_atk and rn not in atk_names:
                            errs.append('%s: 攻击 %r 源可解析但移植后丢失' % (cid, rn))
            for ctx, pt in G.frame_pts(fr):
                rn = pt.props.get('refName')
                src_rn = rev_pt.get(rn, rn)
                if rn and src_rn in src_pt and rn not in pt_names:
                    errs.append('%s: pt %r 源可解析但移植后丢失' % (cid, rn))
                if ctx == 'cre':
                    i1 = G.ival(pt.props.get('int1'), -1)
                    ref = G.ival(pt.props.get('ref'))
                    if i1 >= 0 and ref < 0 and i1 >= n_groups:
                        errs.append('%s: cre.int1 组下标 %d 越界(%d)' % (cid, i1, n_groups))

    # 4) 唯一性：新增肢体名不得与已有重复计数
    name_count = {}
    for cls, pay in new_p.items():
        if cls.endswith('Lmi'):
            f = G.LmiFile.parse(M.unwrap_payload(pay))
            for l in f.limbs:
                nm = l.props.get('name')
                name_count[nm] = name_count.get(nm, 0) + 1
    dup = {k: v for k, v in name_count.items() if v > 1}
    if dup:
        errs.append('新增 LMI 内部重名: %r' % list(dup)[:5])

    return errs, {'limb_space': len(limb_space), 'attacks': len(atk_names),
                  'pts': len(pt_names), 'item_groups': n_groups, 'item_slots': item_slots}


def main():
    chars = sys.argv[1:] or ['lucas']
    print('移植角色:', chars)
    src = load_mod(os.path.join(BASE, 'Promised Mystery of Moisture V-1.9.6.exe'), 'pmom')
    dst = load_mod(os.path.join(BASE, 'HF Eyja 0.1.7.exe'), 'eyja')
    print('源/目标肢体空间: %d / %d' % (len(src.limbs), len(dst.limbs)))

    plan = P.Plan(chars)
    porter = P.Porter(src, dst, plan)
    porter.analyze()
    porter.transform()
    new_p, rep_p, manifest = porter.payloads()
    print('新增载荷: %d, 替换载荷: %d' % (len(new_p), len(rep_p)))
    print('load_sequence 长度:', len(manifest['load_sequence']))
    print('新增音效:', manifest['new_sounds'])

    errs, space = selfcheck(new_p, rep_p, manifest, dst, porter)
    print('空间:', space)
    if errs:
        print('自检错误 %d 条:' % len(errs))
        for e in errs[:25]:
            print('  X', e)
    else:
        print('自检通过 ✓')
    json.dump({'manifest': manifest, 'space': space, 'errors': errs},
              open(os.path.join(BASE, 'work', 'm2_report.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    return not errs


if __name__ == '__main__':
    ok = main()
    sys.exit(0 if ok else 1)
