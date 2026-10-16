# -*- coding: utf-8 -*-
"""钩子 F：独立 itemSpt 的运行时路由。

游戏把所有道具写死在一个包里：`World.AddObjI` 里是
`new ObjI(id, this, Global.sptHash["item"], "", team)`，而 `cre` 只传一个组号过去，
没有任何字段说明该去哪个包取。这一层加的就是那个字段。

数据流（每一步对应下面一个 patch 函数）：

    Frame.cre[].itemSptRoute            <- porter 写进 SPT 载荷（新增 Pt 槽位）
      ObjH::Run / ObjI::Run 生成道具前：world.hfpRoute = this.cre.itemSptRoute
        World.AddObjI：把 hfpRoute 读进局部变量并**立刻清空**，
                       用 Global.ResolveItemSpt(route) 取 Spt，
                       再把 route 盖到新建的 ObjI 上
          World.ReboundBlast：this.hfpRoute = param1.itemSptRoute
                              （反弹爆炸要用道具自己那个包里的 agi）

没有 route 的调用点（剧情道具、地图 dummy、硬编码 iteHash["fire_explode"]、
jenny 被动的 454/90）都读到 null，走目标默认包，与原行为一致。

AVM2 verifier 拒类的表现是启动几秒静默退出、而所有结构检查都是绿的，所以这里每一条
改动都有对应的静态断言，见 verify.py 的 _check_item_route。
"""
from . import abcpatch as A

SLOT_PT_ROUTE = 'itemSptRoute'      # Data.Pt：cre 上的路由（AMF0 载荷里带）
SLOT_OBJI_ROUTE = 'itemSptRoute'    # Game.ObjI：道具实例记住自己属于哪个包
SLOT_WORLD_HANDOFF = 'hfpRoute'     # Game.World：一次性交接槽，AddObjI 读完即清
METHOD_RESOLVE = 'ResolveItemSpt'   # Data.Global 静态方法
SPT_HASH_PREFIX = 'item_'           # Global.sptHash["item_<namespace>"]

# 操作码
_GETLOCAL0 = 0xD0
_GETLOCAL1 = 0xD1
_GETLOCAL = 0x62
_SETLOCAL = 0x63
_PUSHNULL = 0x20
_GETLEX = 0x60
_GETPROPERTY = 0x66
_SETPROPERTY = 0x61
_CALLPROPERTY = 0x46
_CONSTRUCTPROP = 0x4A
_PUSHSCOPE = 0x30
_PUSHSTRING = 0x2C


def _branch_targets(insns):
    out = set()
    for off, op, ops, nxt in insns:
        spec = A.OPCODES[op][1]
        if spec == ['s24']:
            out.add(nxt + ops[0])
        elif spec == ['switch']:
            for rel in ops[:-1]:
                out.add(off + rel)
    return out


def find_cre_addobji_sites(abc, package, cls, method, expected):
    """定位 `world.AddObjI(this.cre.int1, ...)` 的调用点。

    锚点是 `getproperty int1`：这两个方法里各自**恰好** 2 处，每一处后面都紧跟一个
    AddObjI 调用。不用"往回看 N 条里有没有 cre"这种窗口判据 —— 混淆器的 jump 会把
    cre 推到 47~68 条之外，窗口取 45 时 ObjI::Run 实测报 0 处（假阴性），取大了又
    可能误伤旁边的语句。ObjH::Run 一共 52 处 AddObjI（其余是 jenny 被动的 454/90），
    只有这 2 处该路由。
    """
    mi, _ = abc.find_class_method(package, cls, method, static=False)
    if mi is None:
        raise RuntimeError('%s.%s::%s 未找到' % (package, cls, method))
    body = abc.body_of_method(mi)
    insns = list(A.walk_code(body.code))
    int1_sites = [k for k, (o, op, ops, n) in enumerate(insns)
                  if op == _GETPROPERTY and abc.mn_name(ops[0]) == 'int1']
    if len(int1_sites) != expected:
        raise RuntimeError('%s.%s::%s 的 int1 读取点 %d 处，预期 %d 处'
                           % (package, cls, method, len(int1_sites), expected))
    calls = [k for k, (o, op, ops, n) in enumerate(insns)
             if op in (_CALLPROPERTY, 0x4F) and abc.mn_name(ops[0]) == 'AddObjI']
    out = []
    for k in int1_sites:
        cre_mn = None
        for j in range(k - 1, max(-1, k - 14), -1):
            if insns[j][1] == _GETPROPERTY and abc.mn_name(insns[j][2][0]) == 'cre':
                cre_mn = insns[j][2][0]
                break
        if cre_mn is None:
            raise RuntimeError('%s.%s::%s 的 int1@%d 找不到 cre 接收者'
                               % (package, cls, method, k))
        after = [c for c in calls if c > k]
        if not after:
            raise RuntimeError('%s.%s::%s 的 int1@%d 之后没有 AddObjI'
                               % (package, cls, method, k))
        argc = insns[after[0]][2][1]
        if argc != 7:
            raise RuntimeError('AddObjI 参数个数 %d，预期 7' % argc)
        out.append({'int1_index': k, 'cre_mn': cre_mn,
                    'call_index': after[0], 'call_offset': insns[after[0]][0]})
    if len({s['call_index'] for s in out}) != expected:
        raise RuntimeError('%s.%s::%s 的多个 int1 指向了同一个 AddObjI'
                           % (package, cls, method))
    return insns, body, out


def find_world_multiname(abc, package, cls, method):
    """取这个方法自己最常用的那个 `world` 多名。

    裸名字不是身份：ObjH::Run 的 cre 同时有 QName(18062) 和命名空间集
    Multiname(18454)，int1 用的又是 Multiname(18456)。只抄站点实际用的那一个。
    """
    mi, _ = abc.find_class_method(package, cls, method, static=False)
    insns = list(A.walk_code(abc.body_of_method(mi).code))
    tally = {}
    for off, op, ops, nxt in insns:
        if op in (_GETLEX, _GETPROPERTY) and ops and abc.mn_name(ops[0]) == 'world':
            tally[ops[0]] = tally.get(ops[0], 0) + 1
    if not tally:
        raise RuntimeError('%s.%s::%s 里找不到 world 的引用' % (package, cls, method))
    return max(tally.items(), key=lambda kv: kv[1])[0]


# --------------------------------------------------------------- 各个补丁

def add_slots(abc, log):
    """Data.Pt / Game.ObjI / Game.World 三个新槽位。

    Data.Pt 是 sealed（flags 0x09 含 ClassSealed），AMF0 反序列化只能写已声明的属性，
    所以 Pt 上这个槽位不是可选的：不声明，载荷里的 itemSptRoute 会在 LoadAllData 里
    抛异常，而那里有 catch(Error) —— 于是静默中止后面所有加载。
    类型一律 `*`（类型索引 0）：给新槽位标一个解析不到的多名同样会让类被拒。
    """
    out = {
        'pt_route': abc.add_instance_slot('Data', 'Pt', SLOT_PT_ROUTE),
        'obji_route': abc.add_instance_slot('Game', 'ObjI', SLOT_OBJI_ROUTE),
        'world_handoff': abc.add_instance_slot('Game', 'World', SLOT_WORLD_HANDOFF),
    }
    log('   F1 新增槽位: Data.Pt.%s / Game.ObjI.%s / Game.World.%s'
        % (SLOT_PT_ROUTE, SLOT_OBJI_ROUTE, SLOT_WORLD_HANDOFF))
    return out


def _sptHash_shape(abc):
    """从 GenerateIteHash 抄 `sptHash[...]` 的两个多名（它里面就是 sptHash["item"]）。"""
    mi, _ = abc.find_class_method('Data', 'Global', 'GenerateIteHash', static=True)
    if mi is None:
        raise RuntimeError('Data.Global.GenerateIteHash 未找到，无法抄 sptHash 多名')
    src = list(A.walk_code(abc.body_of_method(mi).code))
    for k in range(len(src) - 2):
        a, b, c = src[k:k + 3]
        if (a[1] == _GETLEX and abc.mn_name(a[2][0]) == 'sptHash'
                and b[1] == _PUSHSTRING and abc.strings[b[2][0]] == 'item'
                and c[1] == _GETPROPERTY):
            if abc.multinames[c[2][0]][0] not in (A.MN_MultinameL, A.MN_MultinameLA):
                raise RuntimeError('sptHash[...] 用的不是 MultinameL，形状不符')
            return a[2][0], c[2][0]
    raise RuntimeError('GenerateIteHash 里没找到 sptHash["item"] 的形状')


def add_resolver(abc, log):
    """Data.Global.ResolveItemSpt(route)。

    route 为 null/undefined 时返回目标默认 sptHash["item"]，否则
    sptHash["item_" + route]。作用域深度从同类静态方法抄（Data.Global 静态是 (3,4)，
    写死 1 会让整个类 verify 失败）。方法体只有前向分支。
    """
    spt_hash_mn, mnl_mn = _sptHash_shape(abc)
    asm = A.Asm(abc)
    asm.getlocal(0)
    asm.pushscope()
    asm.getlocal(1)
    asm.pushnull()
    asm.ifne('routed')                     # route != null（松散比较，undefined 也算无）
    asm.getlex(spt_hash_mn)
    asm.pushstring('item')
    asm.getproperty(mnl_mn, name_on_stack=1)
    asm.returnvalue()
    asm.label('routed')
    asm.getlex(spt_hash_mn)
    asm.pushstring(SPT_HASH_PREFIX)
    asm.getlocal(1)
    asm.add()
    asm.getproperty(mnl_mn, name_on_stack=1)
    asm.returnvalue()
    code = asm.finish()
    method_idx, trait, body = abc.add_class_method(
        'Data', 'Global', METHOD_RESOLVE, [0], return_type=0, static=True,
        code=code, max_stack=max(4, asm.max_stack), local_count=2)
    log('   F2 新增静态方法 Data.Global.%s: %d 字节 scope=(%d,%d)'
        % (METHOD_RESOLVE, len(code), body.init_scope_depth, body.max_scope_depth))
    return {'method_index': method_idx, 'code_len': len(code),
            'spt_hash_mn': spt_hash_mn, 'mnl_mn': mnl_mn,
            'scope': [body.init_scope_depth, body.max_scope_depth]}


def patch_add_obji(abc, mn, log):
    """World.AddObjI：按 hfpRoute 取包，并把 route 盖到新 ObjI 上。

    三处改动：
      1. pushscope 之后：this.hfpRoute 读进新局部变量，随后立刻清空 this.hfpRoute。
         先落到局部再用，是为了不依赖"ObjI 构造函数不会再调 AddObjI"这个假设。
      2. `Global.sptHash["item"]` 四条 -> `Global.ResolveItemSpt(local)` 三条。
      3. `setlocal <objI>` 之后：obj.itemSptRoute = local。
    """
    mi, _ = abc.find_class_method('Game', 'World', 'AddObjI', static=False)
    if mi is None:
        raise RuntimeError('Game.World::AddObjI 未找到')
    body = abc.body_of_method(mi)
    if body.exceptions:
        raise RuntimeError('AddObjI 带异常处理块，本补丁未覆盖')
    insns = list(A.walk_code(body.code))

    push_scope = next((k for k, (o, op, ops, n) in enumerate(insns)
                       if op == _PUSHSCOPE), None)
    if push_scope is None:
        raise RuntimeError('AddObjI 里没有 pushscope')

    lookup = None
    for k in range(len(insns) - 3):
        a, b, c, d = insns[k:k + 4]
        if (a[1] == _GETLEX and abc.mn_name(a[2][0]) == 'Global'
                and b[1] == _GETPROPERTY and abc.mn_name(b[2][0]) == 'sptHash'
                and c[1] == _PUSHSTRING and abc.strings[c[2][0]] == 'item'
                and d[1] == _GETPROPERTY):
            if lookup is not None:
                raise RuntimeError('AddObjI 里有多处 sptHash["item"]，形状不符')
            lookup = k
    if lookup is None:
        raise RuntimeError('AddObjI 里没找到 sptHash["item"]')
    data_global_mn = insns[lookup][2][0]

    ctor = next((k for k, (o, op, ops, n) in enumerate(insns)
                 if op == _CONSTRUCTPROP and abc.mn_name(ops[0]) == 'ObjI'), None)
    if ctor is None:
        raise RuntimeError('AddObjI 里没找到 constructprop ObjI')
    store = None
    for k in range(ctor, min(ctor + 4, len(insns))):
        if insns[k][1] == _SETLOCAL:
            store = k
            break
        if insns[k][1] in (0xD4, 0xD5, 0xD6, 0xD7):
            store = k
            break
    if store is None:
        raise RuntimeError('constructprop ObjI 之后没找到 setlocal')
    obj_local = (insns[store][2][0] if insns[store][1] == _SETLOCAL
                 else insns[store][1] - 0xD4)

    dead = [insns[lookup + i][0] for i in (1, 2, 3)]
    clash = sorted(t for t in _branch_targets(insns) if t in dead)
    if clash:
        raise RuntimeError('sptHash["item"] 跨度内有分支目标 %r，不能删除' % clash)

    route_local = body.local_count
    body.local_count += 1

    head = [
        (_GETLOCAL0, []),
        (_GETPROPERTY, [mn['world_handoff']]),
        (_SETLOCAL, [route_local]),
        (_GETLOCAL0, []),
        (_PUSHNULL, []),
        (_SETPROPERTY, [mn['world_handoff']]),
    ]
    resolve = [
        (_GETLEX, [data_global_mn]),
        (_GETLOCAL, [route_local]),
        (_CALLPROPERTY, [mn['resolve'], 1]),
    ]
    stamp = [
        (_GETLOCAL, [obj_local]),
        (_GETLOCAL, [route_local]),
        (_SETPROPERTY, [mn['obji_route']]),
    ]
    A.rewrite_code(
        body,
        after={insns[push_scope][0]: head, insns[store][0]: stamp},
        replace={insns[lookup][0]: resolve, dead[0]: [], dead[1]: [], dead[2]: []},
    )
    body.max_stack += 2
    log('   F3 World.AddObjI: route 存局部 %d，替换 sptHash["item"]，'
        '给 local %d 打标' % (route_local, obj_local))
    return {'route_local': route_local, 'obj_local': obj_local,
            'data_global_mn': data_global_mn}


def patch_rebound_blast(abc, mn, log):
    """World.ReboundBlast：反弹爆炸沿用道具自己那个包。

    `AddObjI(param1.agi, ...)` 的 agi 是道具在**它自己包**里的组号，不设 route 就会
    取到目标包里同号的那个组。
    """
    mi, _ = abc.find_class_method('Game', 'World', 'ReboundBlast', static=False)
    if mi is None:
        raise RuntimeError('Game.World::ReboundBlast 未找到')
    body = abc.body_of_method(mi)
    if body.exceptions:
        raise RuntimeError('ReboundBlast 带异常处理块，本补丁未覆盖')
    insns = list(A.walk_code(body.code))
    calls = [k for k, (o, op, ops, n) in enumerate(insns)
             if op in (_CALLPROPERTY, 0x4F) and abc.mn_name(ops[0]) == 'AddObjI']
    if len(calls) != 1:
        raise RuntimeError('ReboundBlast 里 AddObjI 调用 %d 处，预期 1 处' % len(calls))
    block = [
        (_GETLOCAL0, []),                        # this (World)
        (_GETLOCAL1, []),                        # param1 (ObjI)
        (_GETPROPERTY, [mn['obji_route']]),
        (_SETPROPERTY, [mn['world_handoff']]),
    ]
    A.rewrite_code(body, before={insns[calls[0]][0]: block})
    body.max_stack += 2
    log('   F4 World.ReboundBlast: AddObjI 之前设 hfpRoute')
    return {'call_offset': insns[calls[0]][0]}


def patch_cre_sites(abc, mn, package, cls, method, expected, log):
    """ObjH::Run / ObjI::Run：生成道具前把 cre 的 route 交给 world。

    插在 `callproperty AddObjI` **紧前面**：此时接收者和 7 个实参已在栈上，插入块净栈
    深 0（压 2 弹 2），不动下面那些值。这比"找语句起点"稳 —— 混淆器的 jump+垃圾会把
    语句头推到几十条之外，ObjI::Run 的第二个站点就是这种情况。
    """
    insns, body, sites = find_cre_addobji_sites(abc, package, cls, method, expected)
    if body.exceptions:
        raise RuntimeError('%s.%s::%s 带异常处理块，本补丁未覆盖' % (package, cls, method))
    world_mn = find_world_multiname(abc, package, cls, method)
    before = {}
    for site in sites:
        before[site['call_offset']] = [
            (_GETLEX, [world_mn]),                    # world
            (_GETLOCAL0, []),
            (_GETPROPERTY, [site['cre_mn']]),         # this.cre
            (_GETPROPERTY, [mn['pt_route']]),         # .itemSptRoute
            (_SETPROPERTY, [mn['world_handoff']]),    # world.hfpRoute = ...
        ]
    A.rewrite_code(body, before=before)
    body.max_stack += 2
    log('   F5 %s.%s::%s: %d 个 cre 生成点设 hfpRoute（world mn=%d）'
        % (package, cls, method, len(sites), world_mn))
    return [{'int1_index': s['int1_index'], 'call_offset': s['call_offset'],
             'cre_mn': s['cre_mn'], 'world_mn': world_mn} for s in sites]


def apply_item_route(abc, log=print):
    """按顺序打全部路由补丁，返回可供验证器核对的报告。"""
    slots = add_slots(abc, log)
    resolver = add_resolver(abc, log)
    resolve_mn = abc.qname_idx(0x16, '', METHOD_RESOLVE, add=False)
    if resolve_mn is None:
        raise RuntimeError('新增方法 %s 的 QName 未进常量池' % METHOD_RESOLVE)
    mn = dict(slots)
    mn['resolve'] = resolve_mn
    report = {
        'slot_names': {k: abc.mn_name(v) for k, v in slots.items()},
        'resolver': resolver,
        'resolve_mn': resolve_mn,
    }
    report['add_obji'] = patch_add_obji(abc, mn, log)
    report['rebound_blast'] = patch_rebound_blast(abc, mn, log)
    report['cre_sites'] = {
        'Game.ObjH::Run': patch_cre_sites(abc, mn, 'Game', 'ObjH', 'Run', 2, log),
        'Game.ObjI::Run': patch_cre_sites(abc, mn, 'Game', 'ObjI', 'Run', 2, log),
    }
    return report
