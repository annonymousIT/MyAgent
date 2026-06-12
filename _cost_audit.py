# -*- coding: utf-8 -*-
"""毎ターン非キャッシュで課金される「揮発ブロック」の内訳を実測（API不要）。"""
import core
import json
import profile_store
import tools

ctx = profile_store.context_text()
vol = core.volatile_context()
tools_json = json.dumps(tools.TOOL_DEFS, ensure_ascii=False)


def t(s):
    return int(len(s) / 2.5)


print("=== 揮発ブロック（毎ターン素で課金）===")
for line in ctx.split("\n["):
    name = line.split("]")[0].lstrip("[")
    print(f"  [{name[:34]:36}] {len(line):5}字 ≈{t(line):4}tok")
print(f"  揮発合計: {len(vol)}字 ≈{t(vol)}tok")
print(f"\n=== ツール定義（キャッシュ対象だが冷書込に効く）===")
for d in sorted(tools.TOOL_DEFS, key=lambda x: -len(json.dumps(x))):
    j = json.dumps(d, ensure_ascii=False)
    print(f"  {d['name']:16} {len(j):5}字 ≈{t(j):4}tok")
print(f"  ツール合計: {len(tools_json)}字 ≈{t(tools_json)}tok")
