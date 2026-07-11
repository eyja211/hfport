# -*- coding: utf-8 -*-
"""从 FFDec 反编译文本提取注册数据：pow 字面量、langWords、经验绑定表、charOption。

工具运行时会自动调用 FFDec CLI 导出所需类的 .as 文本（有缓存则跳过）。
"""
import os, re, subprocess

# 常见 Hero Fighter 兼容布局中 c1..c19 对应的角色登录字段序
CANON_C = ['lucas', 'drew', 'shawn', 'jenny', 'taylor', 'leo', 'legge',
           'eason', 'yaga', 'jason', 'gordon', 'giggs', 'titto', 'sinan',
           'iczzy', 'heater', 'raye', 'vivian', 'livermore']


def ensure_exports(base_dir, ffdec_jar, exe_path, out_dir, classes=('Data.Global', 'Web_misc.SelectCharPanel')):
    """确保 out_dir 有反编译文本；返回 {类名: 文本路径}。

    FFDec 不能直接读独立播放器 EXE，需先把内嵌 SWF 抽出为 .swf 再喂给它。
    """
    from . import swf as S
    paths = {}
    missing = []
    for cls in classes:
        p = os.path.join(out_dir, 'scripts', *cls.split('.')) + '.as'
        paths[cls] = p
        if not os.path.exists(p):
            missing.append(cls)
    if missing:
        swf_path = os.path.join(out_dir, '_input.swf')
        os.makedirs(out_dir, exist_ok=True)
        _, swf_bytes = S.read_projector(exe_path)
        # 统一写成未压缩 FWS，FFDec 读取更稳
        f = S.SwfFile.parse(swf_bytes)
        open(swf_path, 'wb').write(f.build())
        cmd = ['java', '-Xmx4g', '-jar', ffdec_jar,
               '-selectclass', ','.join(missing),
               '-export', 'script', out_dir, swf_path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError('FFDec 导出失败: %s' % (r.stderr[-400:] or r.stdout[-400:]))
    for cls, p in paths.items():
        if not os.path.exists(p):
            raise FileNotFoundError(p)
    return paths


_NUM = r'-?\d+(?:\.\d+)?(?:[eE]-?\d+)?'


def _parse_literal_pairs(body):
    """{'k':v, ...} 内部 -> 有序 [(key, value)]，值为 float/str/bool。"""
    pairs = []
    for m in re.finditer(r'"([^"]+)"\s*:\s*("(?:[^"\\]|\\.)*"|%s|true|false)' % _NUM, body):
        k, raw = m.group(1), m.group(2)
        if raw.startswith('"'):
            v = raw[1:-1].encode().decode('unicode_escape')
            # 反编译文本是 UTF-8；unicode_escape 会破坏非 ASCII——改用手工去引号
            v = raw[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        elif raw in ('true', 'false'):
            v = (raw == 'true')
        else:
            v = float(raw)
        pairs.append((k, v))
    return pairs


def extract_pow(text):
    """{slot: [(key,value)...]}（保持字面量键序）"""
    out = {}
    for m in re.finditer(r'pow\["(\w+)"\]\s*=\s*\{(.*?)\};', text, re.S):
        out[m.group(1)] = _parse_literal_pairs(m.group(2))
    return out


def extract_langwords(text, ids=None):
    out = {}
    for m in re.finditer(r'langWords\["([\w#]+)"\]\s*=\s*\{(.*?)\};', text, re.S):
        key = m.group(1)
        if ids is not None and key not in ids:
            continue
        out[key] = _parse_literal_pairs(m.group(2))
    return out


def extract_bindings(text):
    """登录块 pow["slot"].c = cN; -> {slot: N}（后写覆盖前写）"""
    out = {}
    for m in re.finditer(r'pow\["(\w+)"\]\.c\s*=\s*c(\d+);', text):
        out[m.group(1)] = int(m.group(2))
    return out


def extract_charoption(text):
    """SelectCharPanel.as -> [(value:int, name:str, id:str)]"""
    m = re.search(r'charOption:Array\s*=\s*\[(.*?)\];', text, re.S)
    if not m:
        raise ValueError('charOption 未找到')
    out = []
    for mm in re.finditer(r'\{\s*"value":(-?\d+),\s*"name":"((?:[^"\\]|\\.)*)",\s*"id":"(\w+)"\s*\}', m.group(1)):
        out.append((int(mm.group(1)), mm.group(2), mm.group(3)))
    return out


def extract_charlistorder(text):
    """Global.as -> charListOrder 的 id 列表（角色一览的成员）。"""
    m = re.search(r'charListOrder:Array\s*=\s*\[(.*?)\]', text, re.S)
    if not m:
        raise ValueError('charListOrder 未找到')
    return re.findall(r'"([^"]+)"', m.group(1))


def binding_target_map(src_bindings, dst_bindings):
    """源槽位 -> 目标版本中持有同一 cN 的角色 id。

    优先选 cN 的原版角色（CANON_C），并验证它在目标绑定表里确实持有该码；
    否则选目标里任一持有该码的槽位；再不行回退原版角色名。
    """
    dst_holders = {}
    for slot, n in dst_bindings.items():
        dst_holders.setdefault(n, []).append(slot)
    out = {}
    for slot, n in src_bindings.items():
        canon = CANON_C[n - 1] if 1 <= n <= len(CANON_C) else None
        holders = dst_holders.get(n, [])
        if canon and canon in holders:
            out[slot] = canon
        elif holders:
            out[slot] = sorted(holders)[0]
        elif canon:
            out[slot] = canon
        else:
            out[slot] = 'lucas'
    return out
