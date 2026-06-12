"""コスト確定レポート（オフライン・無料）。

costs.jsonl を読んで「1ターンごとの内訳」と「温/冷キャッシュの判定」を出す。
計装後（in/out/cw/cr を持つ行）だけを対象に、ターンまたぎでキャッシュが効いているかを可視化する。

使い方:  .venv/Scripts/python.exe costreport.py
- cr>0 かつ cw==0 … 温（前のターンのキャッシュを読めた＝理想）
- cw>0           … 冷書込が発生（安定部4373tokを書き直し＝高い）。連続ターンで出るなら問題。
"""

from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

# Windows の cp932 コンソールでも ¥ や記号を出せるように標準出力を UTF-8 に。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import core  # 料金倍率・円換算を共有（_PRICE / JPY_PER_USD / _CW_MULT）

COSTS = Path(__file__).parent / "costs.jsonl"


def _rows():
    if not COSTS.exists():
        return []
    out = []
    for line in COSTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    out.sort(key=lambda r: r.get("ts", ""))
    return out


def main() -> None:
    rows = _rows()
    instrumented = [r for r in rows if ("cw" in r and "cr" in r)]
    print(f"全{len(rows)}ターン / 計装済み{len(instrumented)}ターン（cw/crあり）")
    print(f"現在のキャッシュTTL: {core._CACHE_TTL}（書込倍率 {core._PRICE['cw']}x）\n")

    if not instrumented:
        print("計装済みのターンがまだ無い。エージェントで数ターン回してから再実行して。")
        # 旧データだけでも平均は出す
        ys = [float(r.get("yen", 0) or 0) for r in rows]
        if ys:
            print(f"（旧データ）平均 ¥{st.mean(ys):.3f}/ターン  → 1000会話で月¥{st.mean(ys)*1000:.0f}")
        return

    print(f"{'時刻':<20}{'calls':>5}{'in':>7}{'out':>6}{'cw':>7}{'cr':>7}{'yen':>7}  状態")
    warm = cold = 0
    for r in instrumented:
        cw = int(r.get("cw", 0) or 0)
        cr = int(r.get("cr", 0) or 0)
        if cw > 0:
            state = "冷 書込"
            cold += 1
        elif cr > 0:
            state = "温 読込"
            warm += 1
        else:
            state = "無 キャッシュ未使用"
        ts = str(r.get("ts", ""))[-19:].replace("T", " ")
        print(f"{ts:<20}{int(r.get('calls',0)):>5}{int(r.get('in',0)):>7}"
              f"{int(r.get('out',0)):>6}{cw:>7}{cr:>7}{float(r.get('yen',0)):>7.3f}  {state}")

    ys = [float(r.get("yen", 0) or 0) for r in instrumented]
    avg = st.mean(ys)
    print(f"\n温キャッシュ {warm} / 冷書込 {cold}（連続利用なら温が多いほど良い）")
    print(f"計装済み平均: ¥{avg:.3f}/ターン  → 1000会話で月¥{avg*1000:.0f}")
    print(f"目標: ¥0.30/ターン（月¥300）  {'達成' if avg <= 0.30 else 'まだ ' + f'¥{avg-0.30:.3f} 超過'}")

    # 出力トークンの実態（多段呼び出しの主因候補）
    outs = [int(r.get("out", 0) or 0) for r in instrumented]
    perc = [o / max(1, int(r.get("calls", 1) or 1)) for o, r in zip(outs, instrumented)]
    print(f"\n出力トークン: 1ターン平均 {st.mean(outs):.0f} / 1呼び出し平均 {st.mean(perc):.0f}"
          f"（多いほど削減余地。返答は1〜2文=~60tok想定）")


if __name__ == "__main__":
    main()
