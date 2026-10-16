# -*- coding: utf-8 -*-
"""ABC（AVM2 字节码容器）解析/重发射/补丁 + 资产类 DoABC 生成。

策略：完整解析 -> 结构化模型 -> 重发射；以"重发射与原字节完全一致"作为
解析器正确性的硬校验（与 AMF 层同一策略）。修改都是保守的：
  - 常量池只追加不改动（既有索引全部稳定）
  - 方法体只做"末尾 return 重定向 + 追加直线代码"
  - 命名空间条目原位改 kind/name（用于私有->包公有翻转）
"""
import struct

# ------------------------------------------------------------- varint 编解码

def read_u30(d, p):
    v = 0
    shift = 0
    while True:
        b = d[p]
        p += 1
        v |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 35:
            raise ValueError('u30 overflow')
    return v & 0xFFFFFFFF, p


def write_u30(v):
    v &= 0xFFFFFFFF
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def write_s24(v):
    return struct.pack('<i', v)[:3]


def read_s24(d, p):
    b = d[p:p + 3]
    v = b[0] | (b[1] << 8) | (b[2] << 16)
    if v & 0x800000:
        v -= 0x1000000
    return v, p + 3


# ------------------------------------------------------------- 常量池种类

NS_KINDS = {0x05: 'PrivateNs', 0x08: 'Namespace', 0x16: 'PackageNamespace',
            0x17: 'PackageInternalNs', 0x18: 'ProtectedNamespace',
            0x19: 'ExplicitNamespace', 0x1A: 'StaticProtectedNs'}

MN_QName, MN_QNameA = 0x07, 0x0D
MN_RTQName, MN_RTQNameA = 0x0F, 0x10
MN_RTQNameL, MN_RTQNameLA = 0x11, 0x12
MN_Multiname, MN_MultinameA = 0x09, 0x0E
MN_MultinameL, MN_MultinameLA = 0x1B, 0x1C
MN_TypeName = 0x1D


class AbcReader:
    def __init__(self, data):
        self.d = data
        self.p = 0

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self):
        v = struct.unpack_from('<H', self.d, self.p)[0]
        self.p += 2
        return v

    def u30(self):
        v, self.p = read_u30(self.d, self.p)
        return v

    def s32(self):
        v = self.u30()
        if v & 0x80000000:
            v -= 0x100000000
        return v

    def d64(self):
        v = struct.unpack_from('<d', self.d, self.p)[0]
        self.p += 8
        return v

    def raw(self, n):
        b = self.d[self.p:self.p + n]
        self.p += n
        return b


class AbcWriter:
    def __init__(self):
        self.out = bytearray()

    def u8(self, v):
        self.out.append(v & 0xFF)

    def u16(self, v):
        self.out += struct.pack('<H', v)

    def u30(self, v):
        self.out += write_u30(v)

    def s32(self, v):
        self.u30(v & 0xFFFFFFFF)

    def d64(self, v):
        self.out += struct.pack('<d', v)

    def raw(self, b):
        self.out += b


class Trait:
    __slots__ = ('name', 'kind', 'data', 'metadata')

    def __init__(self, name, kind, data, metadata):
        self.name = name        # multiname index
        self.kind = kind        # full kind byte (低4位类型, 高4位属性)
        self.data = data        # tuple, 按类型
        self.metadata = metadata


class MethodBody:
    __slots__ = ('method', 'max_stack', 'local_count', 'init_scope_depth',
                 'max_scope_depth', 'code', 'exceptions', 'traits')

    def __init__(self, method, max_stack, local_count, isd, msd, code, exceptions, traits):
        self.method = method
        self.max_stack = max_stack
        self.local_count = local_count
        self.init_scope_depth = isd
        self.max_scope_depth = msd
        self.code = code
        self.exceptions = exceptions
        self.traits = traits


class AbcFile:
    def __init__(self):
        self.minor = 16
        self.major = 46
        self.ints = [0]
        self.uints = [0]
        self.doubles = [float('nan')]
        self.strings = ['']       # 索引0=""（约定 any）
        self.namespaces = [None]  # (kind, name_idx)
        self.ns_sets = [None]     # [ns_idx...]
        self.multinames = [None]  # (kind, tuple)
        self.methods = []         # dict
        self.metadata = []        # (name_idx, [(k,v)...])
        self.instances = []       # dict
        self.classes = []         # (cinit, [Trait])
        self.scripts = []         # (init, [Trait])
        self.bodies = []          # MethodBody

    # ------------------------------------------------------------- 解析

    @classmethod
    def parse(cls, data):
        r = AbcReader(data)
        a = cls()
        a.minor = r.u16()
        a.major = r.u16()

        n = r.u30()
        a.ints = [0] + [r.s32() for _ in range(max(0, n - 1))]
        n = r.u30()
        a.uints = [0] + [r.u30() for _ in range(max(0, n - 1))]
        n = r.u30()
        a.doubles = [float('nan')] + [r.d64() for _ in range(max(0, n - 1))]
        n = r.u30()
        a.strings = [''] + [r.raw(r.u30()).decode('utf-8', 'surrogateescape')
                            for _ in range(max(0, n - 1))]
        n = r.u30()
        a.namespaces = [None] + [(r.u8(), r.u30()) for _ in range(max(0, n - 1))]
        n = r.u30()
        a.ns_sets = [None]
        for _ in range(max(0, n - 1)):
            c = r.u30()
            a.ns_sets.append([r.u30() for _ in range(c)])
        n = r.u30()
        a.multinames = [None]
        for _ in range(max(0, n - 1)):
            kind = r.u8()
            if kind in (MN_QName, MN_QNameA):
                a.multinames.append((kind, (r.u30(), r.u30())))
            elif kind in (MN_RTQName, MN_RTQNameA):
                a.multinames.append((kind, (r.u30(),)))
            elif kind in (MN_RTQNameL, MN_RTQNameLA):
                a.multinames.append((kind, ()))
            elif kind in (MN_Multiname, MN_MultinameA):
                a.multinames.append((kind, (r.u30(), r.u30())))
            elif kind in (MN_MultinameL, MN_MultinameLA):
                a.multinames.append((kind, (r.u30(),)))
            elif kind == MN_TypeName:
                name = r.u30()
                pc = r.u30()
                a.multinames.append((kind, (name, tuple(r.u30() for _ in range(pc)))))
            else:
                raise ValueError('multiname kind 0x%02X' % kind)

        n = r.u30()
        for _ in range(n):
            pc = r.u30()
            m = {'return_type': r.u30(),
                 'param_types': [r.u30() for _ in range(pc)],
                 'name': r.u30(), 'flags': r.u8()}
            if m['flags'] & 0x08:  # HAS_OPTIONAL
                oc = r.u30()
                m['options'] = [(r.u30(), r.u8()) for _ in range(oc)]
            if m['flags'] & 0x80:  # HAS_PARAM_NAMES
                m['param_names'] = [r.u30() for _ in range(pc)]
            a.methods.append(m)

        n = r.u30()
        for _ in range(n):
            name = r.u30()
            ic = r.u30()
            a.metadata.append((name, [(r.u30(), r.u30()) for _ in range(ic)]))

        def read_traits():
            tc = r.u30()
            ts = []
            for _ in range(tc):
                tname = r.u30()
                tkind = r.u8()
                low = tkind & 0x0F
                if low in (0, 6):      # slot / const
                    data = (r.u30(), r.u30(), r.u30())
                    if data[2] != 0:
                        data = data + (r.u8(),)
                elif low == 4:         # class
                    data = (r.u30(), r.u30())
                elif low == 5:         # function
                    data = (r.u30(), r.u30())
                elif low in (1, 2, 3):  # method/getter/setter
                    data = (r.u30(), r.u30())
                else:
                    raise ValueError('trait kind %d' % low)
                meta = []
                if tkind & 0x40:       # ATTR_Metadata
                    mc = r.u30()
                    meta = [r.u30() for _ in range(mc)]
                ts.append(Trait(tname, tkind, data, meta))
            return ts

        n = r.u30()
        for _ in range(n):
            inst = {'name': r.u30(), 'super': r.u30(), 'flags': r.u8()}
            if inst['flags'] & 0x08:
                inst['protected_ns'] = r.u30()
            ifc = r.u30()
            inst['interfaces'] = [r.u30() for _ in range(ifc)]
            inst['iinit'] = r.u30()
            inst['traits'] = read_traits()
            a.instances.append(inst)
        for _ in range(n):
            a.classes.append((r.u30(), read_traits()))

        n = r.u30()
        for _ in range(n):
            a.scripts.append((r.u30(), read_traits()))

        n = r.u30()
        for _ in range(n):
            method = r.u30()
            ms = r.u30()
            lc = r.u30()
            isd = r.u30()
            msd = r.u30()
            code = bytes(r.raw(r.u30()))
            ec = r.u30()
            excs = [(r.u30(), r.u30(), r.u30(), r.u30(), r.u30()) for _ in range(ec)]
            a.bodies.append(MethodBody(method, ms, lc, isd, msd, code, excs, read_traits()))
        if r.p != len(data):
            raise ValueError('ABC trailing bytes: %d' % (len(data) - r.p))
        return a

    # ------------------------------------------------------------- 发射

    def build(self):
        w = AbcWriter()
        w.u16(self.minor)
        w.u16(self.major)

        w.u30(len(self.ints) if len(self.ints) > 1 else 0)
        for v in self.ints[1:]:
            w.s32(v)
        w.u30(len(self.uints) if len(self.uints) > 1 else 0)
        for v in self.uints[1:]:
            w.u30(v)
        w.u30(len(self.doubles) if len(self.doubles) > 1 else 0)
        for v in self.doubles[1:]:
            w.d64(v)
        w.u30(len(self.strings) if len(self.strings) > 1 else 0)
        for s in self.strings[1:]:
            b = s.encode('utf-8', 'surrogateescape')
            w.u30(len(b))
            w.raw(b)
        w.u30(len(self.namespaces) if len(self.namespaces) > 1 else 0)
        for kind, name in self.namespaces[1:]:
            w.u8(kind)
            w.u30(name)
        w.u30(len(self.ns_sets) if len(self.ns_sets) > 1 else 0)
        for s in self.ns_sets[1:]:
            w.u30(len(s))
            for x in s:
                w.u30(x)
        w.u30(len(self.multinames) if len(self.multinames) > 1 else 0)
        for kind, f in self.multinames[1:]:
            w.u8(kind)
            if kind == MN_TypeName:
                w.u30(f[0])
                w.u30(len(f[1]))
                for x in f[1]:
                    w.u30(x)
            else:
                for x in f:
                    w.u30(x)

        w.u30(len(self.methods))
        for m in self.methods:
            w.u30(len(m['param_types']))
            w.u30(m['return_type'])
            for t in m['param_types']:
                w.u30(t)
            w.u30(m['name'])
            w.u8(m['flags'])
            if m['flags'] & 0x08:
                w.u30(len(m['options']))
                for val, kind in m['options']:
                    w.u30(val)
                    w.u8(kind)
            if m['flags'] & 0x80:
                for pn in m['param_names']:
                    w.u30(pn)

        w.u30(len(self.metadata))
        for name, items in self.metadata:
            w.u30(name)
            w.u30(len(items))
            for k, v in items:
                w.u30(k)
                w.u30(v)

        def write_traits(ts):
            w.u30(len(ts))
            for t in ts:
                w.u30(t.name)
                w.u8(t.kind)
                for pos, x in enumerate(t.data):
                    if pos == 3 and (t.kind & 0x0F) in (0, 6):
                        w.u8(x)  # vkind
                    else:
                        w.u30(x)
                if t.kind & 0x40:
                    w.u30(len(t.metadata))
                    for m in t.metadata:
                        w.u30(m)

        w.u30(len(self.instances))
        for inst in self.instances:
            w.u30(inst['name'])
            w.u30(inst['super'])
            w.u8(inst['flags'])
            if inst['flags'] & 0x08:
                w.u30(inst['protected_ns'])
            w.u30(len(inst['interfaces']))
            for i in inst['interfaces']:
                w.u30(i)
            w.u30(inst['iinit'])
            write_traits(inst['traits'])
        for cinit, ts in self.classes:
            w.u30(cinit)
            write_traits(ts)

        w.u30(len(self.scripts))
        for init, ts in self.scripts:
            w.u30(init)
            write_traits(ts)

        w.u30(len(self.bodies))
        for b in self.bodies:
            w.u30(b.method)
            w.u30(b.max_stack)
            w.u30(b.local_count)
            w.u30(b.init_scope_depth)
            w.u30(b.max_scope_depth)
            w.u30(len(b.code))
            w.raw(b.code)
            w.u30(len(b.exceptions))
            for e in b.exceptions:
                for x in e:
                    w.u30(x)
            write_traits(b.traits)
        return bytes(w.out)

    # ------------------------------------------------------- 常量池辅助

    def str_idx(self, s, add=True):
        try:
            return self.strings.index(s, 1)
        except ValueError:
            if not add:
                raise
            self.strings.append(s)
            return len(self.strings) - 1

    def ns_idx(self, kind, name, add=True):
        ni = self.str_idx(name, add)
        for i in range(1, len(self.namespaces)):
            if self.namespaces[i] == (kind, ni):
                return i
        if not add:
            raise KeyError((kind, name))
        self.namespaces.append((kind, ni))
        return len(self.namespaces) - 1

    def qname_idx(self, ns_kind, ns_name, name, add=True, kind=MN_QName):
        nsi = self.ns_idx(ns_kind, ns_name, add)
        ni = self.str_idx(name, add)
        for i in range(1, len(self.multinames)):
            if self.multinames[i] == (kind, (nsi, ni)):
                return i
        if not add:
            raise KeyError(name)
        self.multinames.append((kind, (nsi, ni)))
        return len(self.multinames) - 1

    def mn_name(self, i):
        """multiname 索引 -> 裸名字（不含命名空间），无名则 None。

        QName 的名字在 f[1]，命名空间集 Multiname 的名字在 f[0]。把两者混起来
        会让 `getproperty int1` 被读成别的字段——ObjH::Run 用的正是 ns-set
        Multiname，而 ObjI::Run 用的是 QName。诊断和锚点都必须走这个函数。
        """
        if i == 0 or self.multinames[i] is None:
            return None
        kind, f = self.multinames[i]
        if kind in (MN_QName, MN_QNameA):
            return self.strings[f[1]]
        if kind in (MN_Multiname, MN_MultinameA):
            return self.strings[f[0]]
        return None

    def mn_str(self, i):
        """multiname 索引 -> 可读名（诊断用）。"""
        if i == 0:
            return '*'
        kind, f = self.multinames[i]
        if kind in (MN_QName, MN_QNameA):
            nsi, ni = f
            ns = self.namespaces[nsi]
            nsname = self.strings[ns[1]] if ns else '?'
            return '%s::%s' % (nsname, self.strings[ni])
        if kind in (MN_Multiname, MN_MultinameA):
            return 'MN:%s' % self.strings[f[0]]
        if kind in (MN_MultinameL, MN_MultinameLA):
            return 'MNL'
        return 'mn_kind_0x%02X' % kind

    def find_instance(self, package, name):
        for idx, inst in enumerate(self.instances):
            mn = self.multinames[inst['name']]
            if mn and mn[0] == MN_QName:
                nsi, ni = mn[1]
                if self.strings[ni] == name:
                    ns = self.namespaces[nsi]
                    if ns and self.strings[ns[1]] == package:
                        return idx
        return None

    def body_of_method(self, method_idx):
        for b in self.bodies:
            if b.method == method_idx:
                return b
        return None

    def find_class_method(self, package, cls_name, method_name, static=True):
        """返回 (method_index, trait)；按 QName 名匹配类的方法 trait。"""
        ci = self.find_instance(package, cls_name)
        if ci is None:
            return None, None
        traits = self.classes[ci][1] if static else self.instances[ci]['traits']
        for t in traits:
            if (t.kind & 0x0F) in (1, 2, 3):
                mn = self.multinames[t.name]
                if mn and mn[0] in (MN_QName, MN_QNameA) and self.strings[mn[1][1]] == method_name:
                    return t.data[1], t
        return None, None

    def find_class_trait(self, package, cls_name, trait_name, static=False,
                         kinds=None):
        """Return a class trait by its exact public QName.

        ``kinds`` contains low-nibble trait kinds (slot=0, method=1, ...).
        This deliberately does not fall back to a bare-name/namespace-set match:
        injected AVM2 must use the QName actually declared by the class.
        """
        ci = self.find_instance(package, cls_name)
        if ci is None:
            return None
        qname = self.qname_idx(0x16, '', trait_name, add=False)
        traits = self.classes[ci][1] if static else self.instances[ci]['traits']
        for trait in traits:
            if trait.name == qname and (kinds is None or (trait.kind & 0x0F) in kinds):
                return trait
        return None

    def add_instance_slot(self, package, cls_name, name, type_package='',
                          type_name='*', trait_kind=0x00):
        """Add one public instance slot and return its QName index.

        slot_id 必须跟着这个类现有的约定走。HF 的 ABC 是 Flex 输出，**每个 slot
        trait 的 slot_id 都是 0**（交给 AVM2 顺序自动分配）；这时候新槽位也必须写 0，
        写 max+1 会和第一个自动分配到的槽位撞号，整个类被 verifier 拒掉（表现是启动
        几秒静默退出，而所有结构检查都是绿的）。只有本来就用显式编号的类才 max+1。
        """
        ci = self.find_instance(package, cls_name)
        if ci is None:
            raise KeyError('%s.%s' % (package, cls_name))
        qname = self.qname_idx(0x16, '', name)
        for trait in self.instances[ci]['traits']:
            if trait.name == qname:
                raise ValueError('%s.%s already declares public trait %s' % (
                    package, cls_name, name
                ))
        existing = [t.data[0] for t in self.instances[ci]['traits']
                    if (t.kind & 0x0F) in (0, 6)]
        slot_id = 0 if (not existing or max(existing) == 0) else max(existing) + 1
        type_qname = (0 if type_name == '*'
                      else self.qname_idx(0x16, type_package, type_name))
        self.instances[ci]['traits'].append(
            Trait(qname, trait_kind, (slot_id, type_qname, 0), [])
        )
        return qname

    def dominant_scope_depths(self, package, cls_name, static=False):
        """(init_scope_depth, max_scope_depth) shared by most methods of a class.

        Returns None when the class declares no method bodies to copy from.
        """
        ci = self.find_instance(package, cls_name)
        if ci is None:
            return None
        traits = self.classes[ci][1] if static else self.instances[ci]['traits']
        tally = {}
        for trait in traits:
            if (trait.kind & 0x0F) not in (1, 2, 3):
                continue
            body = self.body_of_method(trait.data[1])
            if body is None:
                continue
            key = (body.init_scope_depth, body.max_scope_depth)
            tally[key] = tally.get(key, 0) + 1
        if not tally:
            return None
        return max(tally.items(), key=lambda kv: kv[1])[0]

    def add_class_method(self, package, cls_name, name, param_types,
                         return_type=0, static=False, code=b'', max_stack=1,
                         local_count=None, init_scope_depth=None,
                         max_scope_depth=None, flags=0, trait_kind=None):
        """Add a public method trait, method_info and MethodBody.

        Type entries are multiname indices. ``local_count`` includes ``this``
        plus parameters for instance methods, and local0 plus parameters for
        static methods, matching the ABC method-body convention.
        """
        ci = self.find_instance(package, cls_name)
        if ci is None:
            raise KeyError('%s.%s' % (package, cls_name))
        qname = self.qname_idx(0x16, '', name)
        traits = self.classes[ci][1] if static else self.instances[ci]['traits']
        if any(t.name == qname for t in traits):
            raise ValueError('%s.%s already declares public trait %s' % (
                package, cls_name, name
            ))

        method_idx = len(self.methods)
        self.methods.append({
            'return_type': return_type,
            'param_types': list(param_types),
            'name': 0,
            'flags': flags,
        })
        if local_count is None:
            local_count = len(param_types) + 1
        if init_scope_depth is None or max_scope_depth is None:
            # These Flex-nested classes do not enter at depth 1: Data.Global's
            # statics enter at 3, Game.World's instance methods at 4, ObjH's at
            # 5. Declaring the wrong depth is a verify error that kills the whole
            # class, so take it from a sibling of the same class and kind.
            sibling = self.dominant_scope_depths(package, cls_name, static=static)
            if sibling is None:
                raise RuntimeError(
                    '%s.%s 没有可参照的同类方法作用域深度' % (package, cls_name)
                )
            if init_scope_depth is None:
                init_scope_depth = sibling[0]
            if max_scope_depth is None:
                max_scope_depth = sibling[1]
        body = MethodBody(
            method_idx, max_stack, local_count, init_scope_depth,
            max_scope_depth, bytes(code), [], []
        )
        self.bodies.append(body)

        if trait_kind is None:
            trait_kind = 0x11 if static else 0x01
        if static:
            dispatch_id = max(
                (t.data[0] for t in traits
                 if (t.kind & 0x0F) in (1, 2, 3)),
                default=0,
            ) + 1
        else:
            # The target compiler uses disp_id=0 for ordinary instance methods.
            dispatch_id = 0
        trait = Trait(qname, trait_kind, (dispatch_id, method_idx), [])
        traits.append(trait)
        return method_idx, trait, body


# ------------------------------------------------------------- 反汇编支撑

# 操作数格式: 每项为 'u30'/'u8'/'s24'/'switch'
OPCODES = {
    0x01: ('bkpt', []), 0x02: ('nop', []), 0x03: ('throw', []),
    0x04: ('getsuper', ['u30']), 0x05: ('setsuper', ['u30']),
    0x06: ('dxns', ['u30']), 0x07: ('dxnslate', []), 0x08: ('kill', ['u30']),
    0x09: ('label', []),
    0x0C: ('ifnlt', ['s24']), 0x0D: ('ifnle', ['s24']),
    0x0E: ('ifngt', ['s24']), 0x0F: ('ifnge', ['s24']),
    0x10: ('jump', ['s24']), 0x11: ('iftrue', ['s24']), 0x12: ('iffalse', ['s24']),
    0x13: ('ifeq', ['s24']), 0x14: ('ifne', ['s24']), 0x15: ('iflt', ['s24']),
    0x16: ('ifle', ['s24']), 0x17: ('ifgt', ['s24']), 0x18: ('ifge', ['s24']),
    0x19: ('ifstricteq', ['s24']), 0x1A: ('ifstrictne', ['s24']),
    0x1B: ('lookupswitch', ['switch']),
    0x1C: ('pushwith', []), 0x1D: ('popscope', []), 0x1E: ('nextname', []),
    0x1F: ('hasnext', []), 0x20: ('pushnull', []), 0x21: ('pushundefined', []),
    0x23: ('nextvalue', []), 0x24: ('pushbyte', ['u8']), 0x25: ('pushshort', ['u30']),
    0x26: ('pushtrue', []), 0x27: ('pushfalse', []), 0x28: ('pushnan', []),
    0x29: ('pop', []), 0x2A: ('dup', []), 0x2B: ('swap', []),
    0x2C: ('pushstring', ['u30']), 0x2D: ('pushint', ['u30']),
    0x2E: ('pushuint', ['u30']), 0x2F: ('pushdouble', ['u30']),
    0x30: ('pushscope', []), 0x31: ('pushnamespace', ['u30']),
    0x32: ('hasnext2', ['u30', 'u30']),
    0x35: ('li8', []), 0x36: ('li16', []), 0x37: ('li32', []),
    0x38: ('lf32', []), 0x39: ('lf64', []), 0x3A: ('si8', []),
    0x3B: ('si16', []), 0x3C: ('si32', []), 0x3D: ('sf32', []), 0x3E: ('sf64', []),
    0x40: ('newfunction', ['u30']), 0x41: ('call', ['u30']),
    0x42: ('construct', ['u30']), 0x43: ('callmethod', ['u30', 'u30']),
    0x44: ('callstatic', ['u30', 'u30']), 0x45: ('callsuper', ['u30', 'u30']),
    0x46: ('callproperty', ['u30', 'u30']), 0x47: ('returnvoid', []),
    0x48: ('returnvalue', []), 0x49: ('constructsuper', ['u30']),
    0x4A: ('constructprop', ['u30', 'u30']), 0x4C: ('callproplex', ['u30', 'u30']),
    0x4E: ('callsupervoid', ['u30', 'u30']), 0x4F: ('callpropvoid', ['u30', 'u30']),
    0x50: ('sxi1', []), 0x51: ('sxi8', []), 0x52: ('sxi16', []),
    0x53: ('applytype', ['u30']), 0x55: ('newobject', ['u30']),
    0x56: ('newarray', ['u30']), 0x57: ('newactivation', []),
    0x58: ('newclass', ['u30']), 0x59: ('getdescendants', ['u30']),
    0x5A: ('newcatch', ['u30']), 0x5D: ('findpropstrict', ['u30']),
    0x5E: ('findproperty', ['u30']), 0x5F: ('finddef', ['u30']),
    0x60: ('getlex', ['u30']), 0x61: ('setproperty', ['u30']),
    0x62: ('getlocal', ['u30']), 0x63: ('setlocal', ['u30']),
    0x64: ('getglobalscope', []), 0x65: ('getscopeobject', ['u8']),
    0x66: ('getproperty', ['u30']), 0x68: ('initproperty', ['u30']),
    0x6A: ('deleteproperty', ['u30']), 0x6C: ('getslot', ['u30']),
    0x6D: ('setslot', ['u30']), 0x6E: ('getglobalslot', ['u30']),
    0x6F: ('setglobalslot', ['u30']),
    0x70: ('convert_s', []), 0x71: ('esc_xelem', []), 0x72: ('esc_xattr', []),
    0x73: ('convert_i', []), 0x74: ('convert_u', []), 0x75: ('convert_d', []),
    0x76: ('convert_b', []), 0x77: ('convert_o', []), 0x78: ('checkfilter', []),
    0x80: ('coerce', ['u30']), 0x82: ('coerce_a', []), 0x85: ('coerce_s', []),
    0x86: ('astype', ['u30']), 0x87: ('astypelate', []),
    0x90: ('negate', []), 0x91: ('increment', []), 0x92: ('inclocal', ['u30']),
    0x93: ('decrement', []), 0x94: ('declocal', ['u30']), 0x95: ('typeof', []),
    0x96: ('not', []), 0x97: ('bitnot', []),
    0xA0: ('add', []), 0xA1: ('subtract', []), 0xA2: ('multiply', []),
    0xA3: ('divide', []), 0xA4: ('modulo', []), 0xA5: ('lshift', []),
    0xA6: ('rshift', []), 0xA7: ('urshift', []), 0xA8: ('bitand', []),
    0xA9: ('bitor', []), 0xAA: ('bitxor', []), 0xAB: ('equals', []),
    0xAC: ('strictequals', []), 0xAD: ('lessthan', []), 0xAE: ('lessequals', []),
    0xAF: ('greaterthan', []), 0xB0: ('greaterequals', []),
    0xB1: ('instanceof', []), 0xB2: ('istype', ['u30']), 0xB3: ('istypelate', []),
    0xB4: ('in', []),
    0xC0: ('increment_i', []), 0xC1: ('decrement_i', []),
    0xC2: ('inclocal_i', ['u30']), 0xC3: ('declocal_i', ['u30']),
    0xC4: ('negate_i', []), 0xC5: ('add_i', []), 0xC6: ('subtract_i', []),
    0xC7: ('multiply_i', []),
    0xD0: ('getlocal0', []), 0xD1: ('getlocal1', []), 0xD2: ('getlocal2', []),
    0xD3: ('getlocal3', []), 0xD4: ('setlocal0', []), 0xD5: ('setlocal1', []),
    0xD6: ('setlocal2', []), 0xD7: ('setlocal3', []),
    0xEF: ('debug', ['u8', 'u30', 'u8', 'u30']),
    0xF0: ('debugline', ['u30']), 0xF1: ('debugfile', ['u30']),
    0xF2: ('bkptline', ['u30']),
}


def walk_code(code):
    """yield (offset, opcode, operands, next_offset)"""
    p = 0
    n = len(code)
    while p < n:
        start = p
        op = code[p]
        p += 1
        spec = OPCODES.get(op)
        if spec is None:
            raise ValueError('unknown opcode 0x%02X at %d' % (op, start))
        operands = []
        for f in spec[1]:
            if f == 'u30':
                v, p = read_u30(code, p)
                operands.append(v)
            elif f == 'u8':
                operands.append(code[p])
                p += 1
            elif f == 's24':
                v, p = read_s24(code, p)
                operands.append(v)
            elif f == 'switch':
                v, p = read_s24(code, p)
                cc, p = read_u30(code, p)
                cases = [v]
                for _ in range(cc + 1):
                    cv, p = read_s24(code, p)
                    cases.append(cv)
                operands = cases + [cc]
        yield start, op, operands, p


def _encode_instruction(op, operands, branch_values=None):
    """Encode one instruction; branch_values replaces relative branch operands."""
    out = bytearray([op])
    spec = OPCODES[op][1]
    values = list(operands)
    if branch_values is not None:
        values = list(branch_values)
    if spec == ['switch']:
        if not values:
            raise ValueError('lookupswitch without offsets')
        case_count = values[-1]
        rels = values[:-1]
        if len(rels) != case_count + 2:
            raise ValueError('lookupswitch case table length mismatch')
        out += write_s24(rels[0])
        out += write_u30(case_count)
        for rel in rels[1:]:
            out += write_s24(rel)
        return bytes(out)
    for kind, value in zip(spec, values):
        if kind == 'u30':
            out += write_u30(value)
        elif kind == 'u8':
            out.append(value & 0xFF)
        elif kind == 's24':
            out += write_s24(value)
        else:
            raise ValueError('unsupported operand kind %s' % kind)
    if len(values) != len(spec):
        raise ValueError('operand count mismatch for opcode 0x%02X' % op)
    return bytes(out)


def rewrite_code(body, before=None, replace=None, after=None):
    """Re-emit a method after instruction-boundary edits.

    ``before``/``after`` map an old instruction offset to a list of
    ``(opcode, operands)`` tuples. ``replace`` maps an old offset to the same
    shape and replaces that original instruction; an empty list deletes it.
    Inserted/replacement instructions must be branch-free; every original
    branch, lookupswitch and exception range is relocated automatically.

    A branch or exception boundary that pointed at a deleted instruction ends
    up pointing at whatever is emitted next, so only delete instructions that
    belong to an expression nothing jumps into.
    """
    before = before or {}
    replace = replace or {}
    after = after or {}
    parsed = list(walk_code(body.code))
    starts = {off for off, _, _, _ in parsed}
    boundaries = starts | {len(body.code)}
    edit_offsets = set(before) | set(replace) | set(after)
    bad = sorted(edit_offsets - starts)
    if bad:
        raise ValueError('edits are not on instruction boundaries: %r' % bad)

    def normalize(specs, label):
        out = []
        for spec in specs:
            if not (isinstance(spec, tuple) and len(spec) == 2):
                raise TypeError('%s instruction must be (opcode, operands)' % label)
            op, operands = spec
            if op not in OPCODES:
                raise ValueError('unknown inserted opcode 0x%02X' % op)
            if 's24' in OPCODES[op][1] or 'switch' in OPCODES[op][1]:
                raise ValueError('%s may not contain branch instructions' % label)
            out.append((op, list(operands), None))
        return out

    records = []
    old_to_new = {}
    new_size = 0
    for off, op, operands, nxt in parsed:
        # A branch or exception landing on the old instruction must execute an
        # inserted-before block too, so map before adding those records.
        old_to_new[off] = new_size
        for rec in normalize(before.get(off, ()), 'before'):
            records.append(rec)
            new_size += len(_encode_instruction(rec[0], rec[1]))

        if off in replace:
            reps = normalize(replace[off], 'replace')
            for rec in reps:
                records.append(rec)
                new_size += len(_encode_instruction(rec[0], rec[1]))
        else:
            branch_targets = None
            spec = OPCODES[op][1]
            if spec == ['s24']:
                target = nxt + operands[0]
                if target not in boundaries:
                    raise ValueError('branch at %d targets non-boundary %d' % (off, target))
                branch_targets = [target]
            elif spec == ['switch']:
                case_count = operands[-1]
                branch_targets = [off + rel for rel in operands[:-1]]
                if any(target not in boundaries for target in branch_targets):
                    raise ValueError('lookupswitch at %d targets non-boundary' % off)
                branch_targets.append(case_count)
            records.append((op, list(operands), (off, nxt, branch_targets)))
            new_size += len(_encode_instruction(op, operands))

        for rec in normalize(after.get(off, ()), 'after'):
            records.append(rec)
            new_size += len(_encode_instruction(rec[0], rec[1]))
    old_to_new[len(body.code)] = new_size

    emitted = bytearray()
    for op, operands, original in records:
        if original is None or original[2] is None:
            emitted += _encode_instruction(op, operands)
            continue
        old_off, old_next, targets = original
        if OPCODES[op][1] == ['s24']:
            new_next = len(emitted) + 4
            rel = old_to_new[targets[0]] - new_next
            emitted += _encode_instruction(op, operands, [rel])
        elif OPCODES[op][1] == ['switch']:
            new_off = len(emitted)
            case_count = targets[-1]
            rels = [old_to_new[target] - new_off for target in targets[:-1]]
            emitted += _encode_instruction(op, operands, rels + [case_count])
        else:
            emitted += _encode_instruction(op, operands)

    remapped_exceptions = []
    for exc in body.exceptions:
        if not (isinstance(exc, tuple) and len(exc) == 5):
            raise ValueError('exception entry is not a five-tuple: %r' % (exc,))
        frm, to, target, exc_type, var_name = exc
        for value in (frm, to, target):
            if value not in old_to_new:
                raise ValueError('exception boundary %d is not an instruction boundary' % value)
        remapped_exceptions.append((
            old_to_new[frm], old_to_new[to], old_to_new[target],
            exc_type, var_name,
        ))
    body.code = bytes(emitted)
    body.exceptions = remapped_exceptions
    return old_to_new


class Asm:
    """直线代码汇编器：追踪栈深，支持前向标签。"""

    def __init__(self, abc):
        self.abc = abc
        self.out = bytearray()
        self.stack = 0
        self.max_stack = 0
        self._fixups = []   # (pos, label)
        self._labels = {}

    def _op(self, op, *ops, stack_delta=0):
        self.out.append(op)
        spec = OPCODES[op][1]
        for f, v in zip(spec, ops):
            if f == 'u30':
                self.out += write_u30(v)
            elif f == 'u8':
                self.out.append(v & 0xFF)
            elif f == 's24':
                self.out += write_s24(v)
        self.stack += stack_delta
        self.max_stack = max(self.max_stack, self.stack)

    # 常用指令封装
    def getlex(self, mn):
        self._op(0x60, mn, stack_delta=1)

    def getlocal(self, index):
        if 0 <= index <= 3:
            self._op(0xD0 + index, stack_delta=1)
        else:
            self._op(0x62, index, stack_delta=1)

    def setlocal(self, index):
        if 0 <= index <= 3:
            self._op(0xD4 + index, stack_delta=-1)
        else:
            self._op(0x63, index, stack_delta=-1)

    def pushscope(self):
        self._op(0x30, stack_delta=-1)

    def findpropstrict(self, mn):
        self._op(0x5D, mn, stack_delta=1)

    def getproperty(self, mn, name_on_stack=0):
        # 弹 obj(+运行时名) 压值
        self._op(0x66, mn, stack_delta=-name_on_stack)

    def setproperty(self, mn, name_on_stack=0):
        self._op(0x61, mn, stack_delta=-(2 + name_on_stack))

    def initproperty(self, mn, name_on_stack=0):
        self._op(0x68, mn, stack_delta=-(2 + name_on_stack))

    def pushstring(self, s):
        self._op(0x2C, self.abc.str_idx(s), stack_delta=1)

    def pushnum(self, v):
        if isinstance(v, float) and not v.is_integer():
            try:
                di = self.abc.doubles.index(v, 1)
            except ValueError:
                self.abc.doubles.append(v)
                di = len(self.abc.doubles) - 1
            self._op(0x2F, di, stack_delta=1)
            return
        v = int(v)
        if -128 <= v <= 127:
            self._op(0x24, v & 0xFF, stack_delta=1)
        elif -32768 <= v <= 32767:
            self._op(0x25, v & 0xFFFFFFFF, stack_delta=1)
        else:
            try:
                ii = self.abc.ints.index(v, 1)
            except ValueError:
                self.abc.ints.append(v)
                ii = len(self.abc.ints) - 1
            self._op(0x2D, ii, stack_delta=1)

    def pushtrue(self):
        self._op(0x26, stack_delta=1)

    def pushfalse(self):
        self._op(0x27, stack_delta=1)

    def pushnull(self):
        self._op(0x20, stack_delta=1)

    def coerce_s(self):
        self._op(0x85)

    def dup(self):
        self._op(0x2A, stack_delta=1)

    def pop(self):
        self._op(0x29, stack_delta=-1)

    def swap(self):
        self._op(0x2B)

    def newobject(self, n):
        self._op(0x55, n, stack_delta=-(2 * n) + 1)

    def newarray(self, n):
        self._op(0x56, n, stack_delta=-n + 1)

    def construct(self, argn):
        self._op(0x42, argn, stack_delta=-argn)

    def constructprop(self, mn, argn):
        self._op(0x4A, mn, argn, stack_delta=-argn)

    def callproperty(self, mn, argn):
        self._op(0x46, mn, argn, stack_delta=-argn)

    def callpropvoid(self, mn, argn):
        self._op(0x4F, mn, argn, stack_delta=-argn - 1)

    def coerce(self, mn):
        self._op(0x80, mn)

    def add(self):
        self._op(0xA0, stack_delta=-1)

    def throw(self):
        self._op(0x03, stack_delta=-1)

    def returnvoid(self):
        self._op(0x47)

    def returnvalue(self):
        self._op(0x48, stack_delta=-1)

    def jump(self, label):
        self.out.append(0x10)
        self._fixups.append((len(self.out), label))
        self.out += b'\x00\x00\x00'

    def ifeq(self, label):
        # 弹2，相等则跳
        self.stack -= 2
        self.out.append(0x13)
        self._fixups.append((len(self.out), label))
        self.out += b'\x00\x00\x00'

    def ifne(self, label):
        self.stack -= 2
        self.out.append(0x14)
        self._fixups.append((len(self.out), label))
        self.out += b'\x00\x00\x00'

    def iftrue(self, label):
        self.stack -= 1
        self.out.append(0x11)
        self._fixups.append((len(self.out), label))
        self.out += b'\x00\x00\x00'

    def iffalse(self, label):
        self.stack -= 1
        self.out.append(0x12)
        self._fixups.append((len(self.out), label))
        self.out += b'\x00\x00\x00'

    def label(self, name):
        """仅前向跳转目标（无需 label 指令）。"""
        self._labels[name] = len(self.out)

    def finish(self):
        for pos, lab in self._fixups:
            target = self._labels[lab]
            rel = target - (pos + 3)
            self.out[pos:pos + 3] = write_s24(rel)
        return bytes(self.out)


def append_tail(abc, body, asm_code, extra_stack):
    """把 asm_code 追加到方法体末尾：主路径最后一条 return 改为 jump 到尾部，
    尾部执行完以同类 return 收尾。返回描述信息。

    - returnvoid 方法：尾部以 returnvoid 结束
    - returnvalue 方法：跳到尾部时返回值仍在栈上，尾部代码不得动它，结束 returnvalue
    """
    code = body.code
    last_ret_off = None
    last_ret_op = None
    for off, op, _, nxt in walk_code(code):
        if op in (0x47, 0x48) and nxt == len(code):
            last_ret_off = off
            last_ret_op = op
    if last_ret_off is None:
        # 主路径不以 return 结尾（如 throw）——找最后一个 return
        for off, op, _, nxt in walk_code(code):
            if op in (0x47, 0x48):
                last_ret_off, last_ret_op = off, op
        if last_ret_off is None:
            raise ValueError('method has no return instruction')
        if last_ret_off + 1 != len(code):
            raise ValueError('final return not at end of code; unsupported shape')
    # 替换该 return 为 jump -> tail（tail 紧接原代码末尾）
    new = bytearray(code[:last_ret_off])
    jump_pos = len(new)
    new.append(0x10)
    rel = len(code) - (jump_pos + 4)  # 跳到原代码末尾（尾部起点）
    # 原 return 占1字节，jump 占4字节：其后还有 (len(code)-last_ret_off-1) 字节原代码吗?
    # 我们只在 return 是最后一条指令时替换，因此 jump 后直接就是尾部。
    if last_ret_off + 1 != len(code):
        raise ValueError('unexpected trailing bytes after final return')
    rel = 0  # jump 的下一条指令就是尾部
    new += write_s24(rel)
    new += asm_code
    if last_ret_op == 0x47:
        new.append(0x47)
    else:
        new.append(0x48)
    body.code = bytes(new)
    base = 1 if last_ret_op == 0x48 else 0  # 返回值占用的栈
    body.max_stack = max(body.max_stack, base + extra_stack)
    return {'ret_op': last_ret_op, 'tail_len': len(asm_code)}


# ------------------------------------------------------- 资产类 DoABC 生成

def string_usage_count(abc, sidx):
    """统计字符串 sidx 在整个 ABC 里被引用的总次数（用于判断能否原位改字面量）。

    覆盖：pushstring 指令、方法可选参数默认值、trait 的 slot/const 默认值、
    命名空间名、multiname 名、方法名、类/实例名（后几类经由上述结构间接命中）。
    """
    n = 0
    for bd in abc.bodies:
        try:
            for _, op, ops, _ in walk_code(bd.code):
                if op == 0x2C and ops and ops[0] == sidx:  # pushstring
                    n += 1
        except ValueError:
            pass
    for m in abc.methods:
        for val, kind in m.get('options', []):
            if kind == 0x01 and val == sidx:              # 0x01 = Utf8 常量
                n += 1

    def scan_traits(ts):
        c = 0
        for t in ts:
            low = t.kind & 0x0F
            if low in (0, 6) and len(t.data) >= 4 and t.data[3] == 0x01 and t.data[2] == sidx:
                c += 1
        return c

    for inst in abc.instances:
        n += scan_traits(inst['traits'])
    for _, ts in abc.classes:
        n += scan_traits(ts)
    for _, ts in abc.scripts:
        n += scan_traits(ts)
    for bd in abc.bodies:
        n += scan_traits(bd.traits)
    for _, n_idx in abc.namespaces[1:]:
        if n_idx == sidx:
            n += 1
    for mn in abc.multinames[1:]:
        if not mn:
            continue
        kind, fld = mn
        if kind in (MN_QName, MN_QNameA) and fld[1] == sidx:
            n += 1
        elif kind in (MN_RTQName, MN_RTQNameA, MN_Multiname, MN_MultinameA) and fld and fld[0] == sidx:
            n += 1
    for m in abc.methods:
        if m['name'] == sidx:
            n += 1
    for name, items in abc.metadata:
        if name == sidx:
            n += 1
        for k, v in items:
            n += (k == sidx) + (v == sidx)
    return n


def patch_pushbyte_operand(body, old_val, new_val):
    """把方法体里唯一一条 `pushbyte old_val` 的操作数原位改成 new_val。

    要求 0<=new_val<=255（pushbyte 操作数是 1 字节 u8），长度不变，无需重排跳转。
    返回命中次数（应为 1）。
    """
    if not (0 <= new_val <= 255):
        raise ValueError('pushbyte 操作数越界: %d' % new_val)
    hits = []
    for off, op, ops, nxt in walk_code(body.code):
        if op == 0x24 and ops and ops[0] == old_val:  # pushbyte
            hits.append(off + 1)  # 操作数字节位置
    if len(hits) != 1:
        return len(hits)
    code = bytearray(body.code)
    code[hits[0]] = new_val
    body.code = bytes(code)
    return 1


def build_asset_abc(specs):
    """生成自包含 DoABC：每个 spec 一个 (package, name, super_pkg, super_name) 类。

    形状照抄原版资产类（Flex 输出）：
      class <pkg>::<name> extends <super>  flags=0x09(sealed|protectedNs)
      iinit: getlocal0 pushscope getlocal0 constructsuper(0) returnvoid
      cinit: getlocal0 pushscope returnvoid
      script init: 标准 Object->超类 作用域链 + newclass + initproperty
    """
    abc = AbcFile()

    def mn(pkg, name):
        return abc.qname_idx(0x16, pkg, name)

    obj_mn = mn('', 'Object')

    for pkg, name, spkg, sname in specs:
        cls_mn = mn(pkg, name)
        sup_mn = mn(spkg, sname)
        prot_ns = abc.ns_idx(0x18, ('%s:%s' % (pkg, name)) if pkg else name)

        # 三个方法：iinit / cinit / script init（都无参无名）
        def add_method():
            abc.methods.append({'return_type': 0, 'param_types': [], 'name': 0, 'flags': 0})
            return len(abc.methods) - 1

        m_iinit = add_method()
        m_cinit = add_method()
        m_sinit = add_method()

        inst_idx = len(abc.instances)
        abc.instances.append({
            'name': cls_mn, 'super': sup_mn, 'flags': 0x09,
            'protected_ns': prot_ns, 'interfaces': [], 'iinit': m_iinit,
            'traits': [],
        })
        abc.classes.append((m_cinit, []))

        # iinit: getlocal0 pushscope getlocal0 constructsuper 0 returnvoid
        code = bytes([0xD0, 0x30, 0xD0, 0x49, 0x00, 0x47])
        abc.bodies.append(MethodBody(m_iinit, 2, 1, 0, 1, code, [], []))
        # cinit
        code = bytes([0xD0, 0x30, 0x47])
        abc.bodies.append(MethodBody(m_cinit, 2, 1, 0, 1, code, [], []))
        # script init（作用域链: Object -> super；两级足够，超类自身已定义）
        b = bytearray()
        b += bytes([0xD0, 0x30])                    # getlocal0 pushscope
        b += bytes([0x65, 0x00])                    # getscopeobject 0
        b.append(0x60); b += write_u30(obj_mn)      # getlex Object
        b.append(0x30)                              # pushscope
        b.append(0x60); b += write_u30(sup_mn)      # getlex super
        b.append(0x30)                              # pushscope
        b.append(0x60); b += write_u30(sup_mn)      # getlex super
        b.append(0x58); b += write_u30(inst_idx)    # newclass
        b += bytes([0x1D, 0x1D])                    # popscope x2
        b.append(0x68); b += write_u30(cls_mn)      # initproperty
        b.append(0x47)                              # returnvoid
        abc.bodies.append(MethodBody(m_sinit, 3, 1, 1, 5, bytes(b), [], []))

        abc.scripts.append((m_sinit, [Trait(cls_mn, 4, (1, inst_idx), [])]))

    return abc.build()
