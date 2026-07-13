# -*- coding: utf-8 -*-
"""SWF/EXE 容器层：独立播放器 EXE 拆装、SWF 标签读写与组装。

只做字节级操作：未修改的标签原样保留。
"""
import struct, zlib

PROJECTOR_MAGIC = 0xFA123456

# 带 character id 的定义标签（本工程会遇到的类型）
DEF_TAGS_WITH_ID = {2, 14, 20, 21, 22, 32, 35, 36, 37, 39, 46, 87}

TAG_END = 0
TAG_SHOWFRAME = 1
TAG_DEFINESOUND = 14
TAG_SYMBOLCLASS = 76
TAG_DOABC = 82
TAG_DEFINEBINARYDATA = 87


class Tag:
    __slots__ = ('type', 'body', 'long_hdr')

    def __init__(self, ttype, body, long_hdr=None):
        self.type = ttype
        self.body = body  # bytes（不含标签头）
        self.long_hdr = long_hdr  # None=自动; True/False=保留原头形式

    def char_id(self):
        if self.type in DEF_TAGS_WITH_ID and len(self.body) >= 2:
            return struct.unpack_from('<H', self.body, 0)[0]
        return None

    def __repr__(self):
        return 'Tag(%d, %d bytes)' % (self.type, len(self.body))


def emit_tag(tag):
    n = len(tag.body)
    long_hdr = tag.long_hdr
    if long_hdr is None:
        long_hdr = n >= 0x3F
    if long_hdr:
        return struct.pack('<HI', (tag.type << 6) | 0x3F, n) + tag.body
    if n >= 0x3F:
        raise ValueError('short header with body %d bytes' % n)
    return struct.pack('<H', (tag.type << 6) | n) + tag.body


class SwfFile:
    """解析后的 SWF：头部原样保留 + 标签列表。"""

    def __init__(self, sig, version, header_rest, tags):
        self.sig = sig                  # b'FWS'（解压后统一 FWS）
        self.version = version
        self.header_rest = header_rest  # rect + framerate + framecount 原始字节
        self.tags = tags

    @classmethod
    def parse(cls, data):
        sig = data[:3]
        version = data[3]
        if sig == b'CWS':
            data = b'FWS' + data[3:8] + zlib.decompress(data[8:])
            sig = b'FWS'
        elif sig != b'FWS':
            raise ValueError('not a SWF: %r' % sig)
        pos = 8
        nbits = data[pos] >> 3
        rect_bytes = (5 + nbits * 4 + 7) // 8
        header_rest = data[pos:pos + rect_bytes + 4]
        pos += rect_bytes + 4
        tags = []
        while pos < len(data):
            tc = struct.unpack_from('<H', data, pos)[0]
            ttype, tlen, hdr = tc >> 6, tc & 0x3F, 2
            if tlen == 0x3F:
                tlen = struct.unpack_from('<I', data, pos + 2)[0]
                hdr = 6
            tags.append(Tag(ttype, bytes(data[pos + hdr:pos + hdr + tlen]), long_hdr=(hdr == 6)))
            pos += hdr + tlen
            if ttype == TAG_END:
                break
        return cls(sig, version, header_rest, tags)

    def build(self):
        body = bytearray()
        body += self.header_rest
        for t in self.tags:
            body += emit_tag(t)
        total = 8 + len(body)
        return b'FWS' + bytes([self.version]) + struct.pack('<I', total) + bytes(body)

    # ---------------------------------------------------------- 查询辅助

    def find_tags(self, ttype):
        return [(i, t) for i, t in enumerate(self.tags) if t.type == ttype]

    def symbol_classes(self):
        """[(tag_index, {char_id: class_name})]"""
        out = []
        for i, t in self.find_tags(TAG_SYMBOLCLASS):
            out.append((i, parse_symbolclass(t.body)))
        return out

    def class_to_char_id(self):
        m = {}
        for _, syms in self.symbol_classes():
            for cid, name in syms.items():
                m[name] = cid
        return m

    def binary_payload(self, class_name):
        cid = self.class_to_char_id()[class_name]
        for _, t in self.find_tags(TAG_DEFINEBINARYDATA):
            if t.char_id() == cid:
                return t.body[6:]
        raise KeyError(class_name)

    def max_char_id(self):
        mx = 0
        for t in self.tags:
            cid = t.char_id()
            if cid:
                mx = max(mx, cid)
        for _, syms in self.symbol_classes():
            if syms:
                mx = max(mx, max(syms))
        return mx

    # ---------------------------------------------------------- 修改操作

    def replace_binary(self, class_name, payload):
        cid = self.class_to_char_id()[class_name]
        for i, t in self.find_tags(TAG_DEFINEBINARYDATA):
            if t.char_id() == cid:
                self.tags[i] = Tag(TAG_DEFINEBINARYDATA,
                                   struct.pack('<HI', cid, 0) + payload,
                                   long_hdr=True)
                return
        raise KeyError(class_name)

    def add_binary(self, char_id, payload):
        t = Tag(TAG_DEFINEBINARYDATA, struct.pack('<HI', char_id, 0) + payload)
        return t

    def add_symbolclass_entries(self, tag_index, entries):
        """entries: [(char_id, class_name)] 追加进指定 SymbolClass 标签。"""
        t = self.tags[tag_index]
        n = struct.unpack_from('<H', t.body, 0)[0]
        body = bytearray(t.body)
        body[0:2] = struct.pack('<H', n + len(entries))
        for cid, name in entries:
            body += struct.pack('<H', cid) + name.encode('utf-8') + b'\x00'
        self.tags[tag_index] = Tag(TAG_SYMBOLCLASS, bytes(body))

    def insert_tags(self, index, new_tags):
        self.tags[index:index] = new_tags


def parse_symbolclass(body):
    n = struct.unpack_from('<H', body, 0)[0]
    p = 2
    out = {}
    for _ in range(n):
        cid = struct.unpack_from('<H', body, p)[0]
        p += 2
        e = body.index(b'\x00', p)
        out[cid] = body[p:e].decode('utf-8', 'replace')
        p = e + 1
    return out


def make_doabc_tag(abc_bytes, name='hfport', lazy=True):
    body = struct.pack('<I', 1 if lazy else 0) + name.encode('utf-8') + b'\x00' + abc_bytes
    return Tag(TAG_DOABC, body)


def parse_doabc(body):
    """DoABC 标签体 -> (flags, name, abc_bytes)"""
    flags = struct.unpack_from('<I', body, 0)[0]
    e = body.index(b'\x00', 4)
    name = body[4:e].decode('utf-8', 'replace')
    return flags, name, body[e + 1:]


# ---------------------------------------------------------------- EXE 层

def read_projector(path):
    """EXE -> (exe_prefix_bytes, swf_bytes)。也接受纯 SWF 文件。"""
    with open(path, 'rb') as f:
        data = f.read()
    if data[:3] in (b'FWS', b'CWS'):
        return b'', data
    if data[:2] != b'MZ':
        raise ValueError('不是 EXE 也不是 SWF: %s' % path)
    magic, ln = struct.unpack_from('<II', data, len(data) - 8)
    if magic != PROJECTOR_MAGIC:
        raise ValueError('EXE 里没有找到 Flash 播放器页脚（不是独立播放器 EXE？）: %s' % path)
    off = len(data) - 8 - ln
    return data[:off], data[off:off + ln]


def write_projector(path, exe_prefix, swf_bytes):
    with open(path, 'wb') as f:
        f.write(exe_prefix)
        f.write(swf_bytes)
        f.write(struct.pack('<II', PROJECTOR_MAGIC, len(swf_bytes)))
