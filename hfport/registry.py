# -*- coding: utf-8 -*-
"""从 FFDec 反编译文本提取版本注册清单。

工具运行时会自动调用 FFDec CLI 导出所需类的 .as 文本（有缓存则跳过）。
"""
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass

# 常见 Hero Fighter 兼容布局中 c1..c19 对应的角色登录字段序
CANON_C = ['lucas', 'drew', 'shawn', 'jenny', 'taylor', 'leo', 'legge',
           'eason', 'yaga', 'jason', 'gordon', 'giggs', 'titto', 'sinan',
           'iczzy', 'heater', 'raye', 'vivian', 'livermore']


@dataclass
class VersionInventory:
    pow: dict
    langwords: dict
    char_options: list
    char_list_order: list
    binding_codes: dict
    sound_classes: dict


def export_cache_key(exe_path):
    h = hashlib.sha256()
    with open(exe_path, 'rb') as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


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


def extract_binding_links(text):
    """追加块 pow["slot"].c = pow["other"].c; -> {slot: other}。"""
    out = {}
    pat = r'pow\["([\w#]+)"\]\.c\s*=\s*pow\["([\w#]+)"\]\.c\s*;'
    for m in re.finditer(pat, text):
        out[m.group(1)] = m.group(2)
    return out


def resolve_binding_codes(direct, links):
    """把角色间绑定链解析为最终 cN；循环或断链不写入结果。"""
    out = dict(direct)
    for slot in links:
        seen = set()
        cur = slot
        while cur not in out and cur in links and cur not in seen:
            seen.add(cur)
            cur = links[cur]
        if cur in out:
            out[slot] = out[cur]
    return out


def choose_binding_target(source_slot, source_code, target_codes):
    holders = sorted(
        slot for slot, code in target_codes.items() if code == source_code
    )
    canon = CANON_C[source_code - 1] if 1 <= source_code <= len(CANON_C) else None
    if canon in holders:
        return canon
    if holders:
        return holders[0]
    if source_slot in target_codes:
        return source_slot
    if canon in target_codes:
        return canon
    return None


def extract_charoption(text):
    """SelectCharPanel.as -> [(value:int, name:str, id:str)]"""
    m = re.search(r'charOption:Array\s*=\s*\[(.*?)\];', text, re.S)
    if not m:
        raise ValueError('charOption 未找到')
    out = []
    for mm in re.finditer(r'\{\s*"value":(-?\d+),\s*"name":"((?:[^"\\]|\\.)*)",\s*"id":"(\w+)"\s*\}', m.group(1)):
        out.append((int(mm.group(1)), mm.group(2), mm.group(3)))
    for mm in re.finditer(r'charOption\.push\s*\(\s*\{(.*?)\}\s*\)\s*;', text, re.S):
        pairs = dict(_parse_literal_pairs(mm.group(1)))
        if all(k in pairs for k in ('value', 'name', 'id')):
            out.append((int(pairs['value']), pairs['name'], pairs['id']))
    return out


def extract_charlistorder(text):
    """Global.as -> charListOrder 的 id 列表（角色一览的成员）。"""
    m = re.search(r'charListOrder:Array\s*=\s*\[(.*?)\]', text, re.S)
    if not m:
        raise ValueError('charListOrder 未找到')
    out = re.findall(r'"([^"]+)"', m.group(1))
    out.extend(re.findall(r'charListOrder\.push\s*\(\s*"([^"]+)"\s*\)\s*;', text))
    return out


def build_inventory(global_text, select_text):
    direct = extract_bindings(global_text)
    links = extract_binding_links(global_text)
    return VersionInventory(
        pow=extract_pow(global_text),
        langwords=extract_langwords(global_text),
        char_options=extract_charoption(select_text),
        char_list_order=extract_charlistorder(global_text),
        binding_codes=resolve_binding_codes(direct, links),
        sound_classes=extract_sound_classes(global_text),
    )


def extract_sound_classes(text):
    out = {}
    pattern = (
        r'Console\.sfx\[(\d+)\]\s*=\s*new\s+(?:Game\.)?Sfx'
        r'\s*\(\s*(?:new\s+)?([A-Za-z_]\w*)(?:\(\))?'
    )
    for m in re.finditer(pattern, text):
        out[int(m.group(1))] = m.group(2)
    return out


def load_inventory(work_dir, ffdec_jar, exe_path):
    key = export_cache_key(exe_path)
    out_dir = os.path.join(work_dir, 'registry', key)
    paths = ensure_exports(
        work_dir,
        ffdec_jar,
        exe_path,
        out_dir,
        classes=('Data.Global', 'Web_misc.SelectCharPanel'),
    )
    with open(paths['Data.Global'], encoding='utf-8') as f:
        global_text = f.read()
    with open(paths['Web_misc.SelectCharPanel'], encoding='utf-8') as f:
        select_text = f.read()
    return build_inventory(global_text, select_text)


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
