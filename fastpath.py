"""高速パス：頻出コマンドをコードで判定し LLM を介さず即実行（ADR-0043）。

声で頻出する明確な操作（起動 / 閉じる / しまう / 出す / 音量）を、末尾動詞＋既存リゾルバで
コード判定し、LLM 0回・即時・¥0 で実行する。これによりレイテンシ・コスト・Haikuレート制限を同時に下げる。
少しでも曖昧（質問・状態確認・複合・対象が解決しない・配置系）なら None を返し、通常の LLM パス
（core.run_turn）へ委ねる＝汎用性は LLM が担保する（高速パスは“追い越し車線”であって置換ではない）。

設計（ADR-0043）:
- match(text) は意図の解析だけ（副作用なし＝オフラインで全テスト可能。Windows API も叩かない）。
- run(intent) が tools 経由で実行。succeeded(result) が成否を判定。
- voice.py は match→(hitなら) run し、成功時のみ相槌、失敗時は結果を正直に喋る（嘘報告しない）。
"""
from __future__ import annotations

import tools

# 末尾の動詞 → 操作。長いものから照合する（「起動して」を「起動」より先に拾う）。
_V_LAUNCH = ("立ち上げて", "立ち上げ", "起動して", "起動", "ひらいて", "開いて", "つけて")
_V_CLOSE = ("閉じといて", "閉じておいて", "閉じて", "終了して", "終了", "落として")
_V_MIN = ("しまっといて", "しまっておいて", "しまって", "どけて", "隠して", "最小化して", "最小化")
_V_RESTORE = ("呼び戻して", "戻して", "復元して", "復元", "出して")  # 出す＝最小化なら復元/未起動なら起動
_ALL_VERBS = _V_LAUNCH + _V_CLOSE + _V_MIN + _V_RESTORE

# 高速パスに乗せない語（1つでも含めば LLM へ退避＝誤爆より「LLMで2-3秒待つ」を選ぶ）。
_BAIL = ("?", "？", "てる", "てた", "てます", "てない", "どう", "かな", "っけ", "ってる",
         "けど", "から", "なら", "たら", "教えて", "って言", "どれ", "どっち", "全部")
_SEPS = ("、", "，", ",")
_NAME_TRIM = "をってのはねよ　 「」『』、，,。."  # 名前候補の前後から除く助詞・区切り・句読点


def _trailing(t: str, verbs):
    """t が verbs のいずれかで終わるなら、その手前（＝対象名候補）を助詞・区切りを除いて返す。無ければ None。"""
    for v in sorted(verbs, key=len, reverse=True):
        if t.endswith(v):
            return t[: -len(v)].strip(_NAME_TRIM)
    return None


def match(text: str) -> "dict | None":
    """発話 → 高速パス意図 dict、または None（LLM へ）。副作用なし。

    返す dict: {"kind": launch/close/minimize/restore/volume, "name": 対象, "sub": 音量の向き}
    """
    # 末尾の句読点・空白だけ除去（助詞「て/の…」を消すと動詞末尾を食うので _PARTICLES は使わない。
    # 助詞除去は名前抽出時の _trailing 内だけで行う）。
    t = (text or "").strip().strip("。.　 ")
    if not t:
        return None
    if any(b in t for b in _BAIL):                 # 質問・状態確認・曖昧 → LLM
        return None
    for sep in _SEPS:                              # 複合（区切りの前にも動作がある）→ LLM
        if sep in t and any(v in t.split(sep)[0] for v in _ALL_VERBS):
            return None

    # 音量（対象名は不要）
    if "音量" in t or "ボリューム" in t:
        if any(w in t for w in ("上げ", "あげ", "大きく", "でかく", "アップ")):
            return {"kind": "volume", "sub": "volume_up"}
        if any(w in t for w in ("下げ", "さげ", "小さく", "しぼ", "ダウン")):
            return {"kind": "volume", "sub": "volume_down"}
        if any(w in t for w in ("ミュート", "消音", "黙らせ")):
            return {"kind": "volume", "sub": "mute"}
        return None  # 「音量どう？」等は質問なので LLM

    # アプリ＋末尾動詞。対象が既存アプリとして解決できる時だけ高速パス。
    apps = tools.load_config().get("apps", {})
    for verbs, kind in ((_V_LAUNCH, "launch"), (_V_CLOSE, "close"),
                        (_V_MIN, "minimize"), (_V_RESTORE, "restore")):
        name = _trailing(t, verbs)
        if name is None:
            continue
        if not name or not tools._app_matches(apps, name):
            return None  # 動詞は合うが対象が解決しない＝間接表現/曖昧 → LLM へ
        return {"kind": kind, "name": name}
    return None


_FAIL_MARKERS = ("見つかりません", "失敗", "開いていない", "開いてない", "ありません",
                 "未対応", "できませんでした", "追従しません")


def succeeded(result: str) -> bool:
    """run() の結果文字列が成功を示すか（相槌を返すか、正直に結果を喋るかの判定）。"""
    return bool(result) and not any(m in result for m in _FAIL_MARKERS)


def run(intent: dict) -> str:
    """意図を tools 経由で実行し、結果文字列を返す。"""
    kind = intent["kind"]
    if kind == "volume":
        return tools.run_system(intent["sub"])
    name = intent["name"]
    if kind == "launch":
        return tools.launch_app(name)
    if kind == "close":
        return tools.close_app(name)
    if kind == "minimize":
        return tools.run_tool("manage_window", {"action": "minimize", "app": name})
    if kind == "restore":
        # 出す＝最小化中なら復元・前面化／未起動なら起動。restore を試し、窓が無ければ起動に切替。
        res = tools.run_tool("manage_window", {"action": "restore", "app": name})
        if not succeeded(res):
            res = tools.launch_app(name)
        return res
    return f"未対応の高速パス: {kind}"
