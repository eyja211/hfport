# -*- coding: utf-8 -*-
"""AMF0/AMF3 编解码 + HF 数据外层流（writeUTF/writeInt/writeObject）。

格式栈（已对两个 mod 全部数据字节级往返验证）：
  DefineBinaryData 载荷 = AMF3 writeObject(ByteArray) = 0x0C + U29(len<<1|1) + zlib
  解压后内层对象 = AMF0 类型化对象流（registerClassAlias 的 Data.* 类）

解码保留属性顺序与引用结构，保证 decode->encode 字节一致。
"""
import struct, zlib


class AmfObj:
    """类型化(alias!=None)或匿名 AMF0 对象；保留键序。"""
    __slots__ = ('alias', 'props')

    def __init__(self, alias=None, props=None):
        self.alias = alias
        self.props = props if props is not None else {}

    def __repr__(self):
        return 'AmfObj(%s, %d props)' % (self.alias, len(self.props))


class AmfEcmaArray:
    """AMF0 ECMA 数组：count + 有序键值对。"""
    __slots__ = ('count', 'props')

    def __init__(self, count=0, props=None):
        self.count = count
        self.props = props if props is not None else {}

    def __repr__(self):
        return 'AmfEcmaArray(count=%d, %d props)' % (self.count, len(self.props))


class AmfStrictArray:
    __slots__ = ('items',)

    def __init__(self, items=None):
        self.items = items if items is not None else []


class AmfDate:
    __slots__ = ('ms', 'tz')

    def __init__(self, ms=0.0, tz=0):
        self.ms = ms
        self.tz = tz


class Undefined:
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __repr__(self):
        return 'undefined'


UNDEFINED = Undefined()


class Amf0Reader:
    def __init__(self, data, pos=0):
        self.d = data
        self.p = pos
        self.refs = []

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self):
        v = struct.unpack_from('>H', self.d, self.p)[0]
        self.p += 2
        return v

    def u32(self):
        v = struct.unpack_from('>I', self.d, self.p)[0]
        self.p += 4
        return v

    def dbl(self):
        v = struct.unpack_from('>d', self.d, self.p)[0]
        self.p += 8
        return v

    def utf(self):
        n = self.u16()
        s = self.d[self.p:self.p + n].decode('utf-8')
        self.p += n
        return s

    def read(self):
        m = self.u8()
        if m == 0x00:
            return self.dbl()
        if m == 0x01:
            return self.u8() != 0
        if m == 0x02:
            return self.utf()
        if m == 0x03:
            o = AmfObj(None)
            self.refs.append(o)
            self._read_props(o.props)
            return o
        if m == 0x05:
            return None
        if m == 0x06:
            return UNDEFINED
        if m == 0x07:
            return self.refs[self.u16()]
        if m == 0x08:
            a = AmfEcmaArray(0)
            self.refs.append(a)
            a.count = self.u32()
            self._read_props(a.props)
            return a
        if m == 0x0A:
            a = AmfStrictArray()
            self.refs.append(a)
            n = self.u32()
            for _ in range(n):
                a.items.append(self.read())
            return a
        if m == 0x0B:
            dt = AmfDate()
            dt.ms = self.dbl()
            dt.tz = self.u16()
            return dt
        if m == 0x0C:
            n = self.u32()
            s = self.d[self.p:self.p + n].decode('utf-8')
            self.p += n
            return s
        if m == 0x10:
            alias = self.utf()
            o = AmfObj(alias)
            self.refs.append(o)
            self._read_props(o.props)
            return o
        raise ValueError('AMF0 marker 0x%02X at %d' % (m, self.p - 1))

    def _read_props(self, props):
        while True:
            key = self.utf()
            if key == '' and self.d[self.p] == 0x09:
                self.p += 1
                return
            props[key] = self.read()


class Amf0Writer:
    def __init__(self):
        self.out = bytearray()
        self.refs = {}

    def utf(self, s):
        b = s.encode('utf-8')
        self.out += struct.pack('>H', len(b)) + b

    def write(self, v):
        o = self.out
        if v is None:
            o.append(0x05)
        elif v is UNDEFINED:
            o.append(0x06)
        elif isinstance(v, bool):
            o.append(0x01)
            o.append(1 if v else 0)
        elif isinstance(v, (int, float)):
            o.append(0x00)
            o += struct.pack('>d', float(v))
        elif isinstance(v, str):
            b = v.encode('utf-8')
            if len(b) > 0xFFFF:
                o.append(0x0C)
                o += struct.pack('>I', len(b)) + b
            else:
                o.append(0x02)
                o += struct.pack('>H', len(b)) + b
        elif isinstance(v, (AmfObj, AmfEcmaArray, AmfStrictArray)):
            ref = self.refs.get(id(v))
            if ref is not None:
                o.append(0x07)
                o += struct.pack('>H', ref)
                return
            if len(self.refs) <= 0xFFFF:
                self.refs[id(v)] = len(self.refs)
            if isinstance(v, AmfEcmaArray):
                o.append(0x08)
                o += struct.pack('>I', v.count)
                self._write_props(v.props)
            elif isinstance(v, AmfStrictArray):
                o.append(0x0A)
                o += struct.pack('>I', len(v.items))
                for it in v.items:
                    self.write(it)
            elif v.alias is None:
                o.append(0x03)
                self._write_props(v.props)
            else:
                o.append(0x10)
                self.utf(v.alias)
                self._write_props(v.props)
        elif isinstance(v, AmfDate):
            o.append(0x0B)
            o += struct.pack('>d', v.ms) + struct.pack('>H', v.tz)
        else:
            raise TypeError('cannot write %r' % (type(v),))

    def _write_props(self, props):
        for k, val in props.items():
            self.utf(k)
            self.write(val)
        self.out += b'\x00\x00\x09'


def amf0_decode(data):
    r = Amf0Reader(data)
    v = r.read()
    if r.p != len(data):
        raise ValueError('trailing %d bytes' % (len(data) - r.p))
    return v


def amf0_encode(value):
    w = Amf0Writer()
    w.write(value)
    return bytes(w.out)


def deep_clone(v, memo=None):
    """克隆 AMF 对象图，保留共享引用结构。"""
    if memo is None:
        memo = {}
    if isinstance(v, (AmfObj, AmfEcmaArray, AmfStrictArray)):
        got = memo.get(id(v))
        if got is not None:
            return got
        if isinstance(v, AmfObj):
            c = AmfObj(v.alias)
            memo[id(v)] = c
            for k, x in v.props.items():
                c.props[k] = deep_clone(x, memo)
        elif isinstance(v, AmfEcmaArray):
            c = AmfEcmaArray(v.count)
            memo[id(v)] = c
            for k, x in v.props.items():
                c.props[k] = deep_clone(x, memo)
        else:
            c = AmfStrictArray()
            memo[id(v)] = c
            c.items = [deep_clone(x, memo) for x in v.items]
        return c
    if isinstance(v, AmfDate):
        return AmfDate(v.ms, v.tz)
    return v


# ---------------------------------------------------------------- AMF3 外层

def read_u29(d, p):
    v = 0
    for i in range(4):
        c = d[p]
        p += 1
        if i < 3:
            v = (v << 7) | (c & 0x7F)
            if not (c & 0x80):
                break
        else:
            v = (v << 8) | c
    return v, p


def write_u29(v):
    out = bytearray()
    if v < 0x80:
        out.append(v)
    elif v < 0x4000:
        out += bytes(((v >> 7) | 0x80, v & 0x7F))
    elif v < 0x200000:
        out += bytes(((v >> 14) | 0x80, ((v >> 7) & 0x7F) | 0x80, v & 0x7F))
    else:
        out += bytes(((v >> 22) | 0x80, ((v >> 15) & 0x7F) | 0x80,
                      ((v >> 8) & 0x7F) | 0x80, v & 0xFF))
    return bytes(out)


class StreamReader:
    """读 writeUTF/writeInt/writeObject 流（objectEncoding=AMF3）。"""

    def __init__(self, data, pos=0):
        self.d = data
        self.p = pos

    def utf(self):
        n = struct.unpack_from('>H', self.d, self.p)[0]
        s = self.d[self.p + 2:self.p + 2 + n].decode('utf-8')
        self.p += 2 + n
        return s

    def i32(self):
        v = struct.unpack_from('>i', self.d, self.p)[0]
        self.p += 4
        return v

    def obj_bytes(self):
        """AMF3 readObject，只支持 ByteArray|null（游戏只用这两种）。"""
        m = self.d[self.p]
        self.p += 1
        if m == 0x01:
            return None
        if m != 0x0C:
            raise ValueError('AMF3 marker 0x%02X at %d (expect ByteArray)' % (m, self.p - 1))
        v, self.p = read_u29(self.d, self.p)
        if not (v & 1):
            raise ValueError('AMF3 ByteArray by-reference not supported')
        n = v >> 1
        b = self.d[self.p:self.p + n]
        self.p += n
        return b

    def eof(self):
        return self.p >= len(self.d)


class StreamWriter:
    def __init__(self):
        self.out = bytearray()

    def utf(self, s):
        b = s.encode('utf-8')
        self.out += struct.pack('>H', len(b)) + b

    def i32(self, v):
        self.out += struct.pack('>i', v)

    def obj_bytes(self, b):
        if b is None:
            self.out.append(0x01)
        else:
            self.out.append(0x0C)
            self.out += write_u29((len(b) << 1) | 1)
            self.out += b


def unwrap_payload(payload):
    """DefineBinaryData 载荷 -> 解压后的内层流字节。"""
    sr = StreamReader(payload)
    b = sr.obj_bytes()
    if not sr.eof():
        raise ValueError('trailing bytes after outer ByteArray')
    return zlib.decompress(b)


def wrap_payload(stream_bytes, level=9):
    sw = StreamWriter()
    sw.obj_bytes(zlib.compress(stream_bytes, level))
    return bytes(sw.out)
