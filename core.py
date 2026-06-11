"""MyAgent のコア処理（UIから分離）。

agent.py / web.py の両方から使える「APIキー読み込み・人格読み込み・1ターン処理」をまとめた層。
UI（ターミナル / ブラウザ）が変わっても、ここは無変更で使い回せる。
"""

from __future__ import annotations

import os
from pathlib import Path

import anthropic

import tools

MODEL = "claude-haiku-4-5"
BASE = Path(__file__).parent
PERSONA_PATH = BASE / "persona.txt"
ENV_PATH = BASE / ".env"


def load_api_key() -> "str | None":
    """環境変数を優先。無ければ .env から ANTHROPIC_API_KEY を拾う。"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "ANTHROPIC_API_KEY" in line and "=" in line:
                val = line.split("=", 1)[1].strip()
                return val.strip('"').strip("'")
    return None


def load_persona() -> str:
    return PERSONA_PATH.read_text(encoding="utf-8").strip()


def build_client() -> anthropic.Anthropic:
    key = load_api_key()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY が見つかりません（環境変数か .env を確認）。")
    return anthropic.Anthropic(api_key=key)


def _text_of(response) -> str:
    parts = [b.text for b in response.content if b.type == "text"]
    return "".join(parts).strip() or "（…）"


def _app_label(name: str, entry) -> str:
    """apps の1項目を「表示名(主要エイリアス)」の表記に整える。

    新スキーマ（dict）なら aliases を最大2件まで併記し、LLM がエイリアスでも
    引けることを明示する。旧スキーマ（str）なら表示名のみ。
    """
    if isinstance(entry, dict):
        aliases = entry.get("aliases") or []
        if aliases:
            return f"{name}({'・'.join(aliases[:2])})"
    return name


def available_operations() -> str:
    """②材料：現在のconfigから「いま実行できる操作の名前候補」を組み立てる。

    これをシステムプロンプトに同梱することで、モデルが自分に何ができるかを把握し、
    曖昧な指示の解釈・候補が複数のときの聞き返し・無い操作の正直な拒否ができるようになる。
    マージ後の2層構造（ADR-0011）に追従：apps は「表示名(主要エイリアス)」で列挙する。
    """
    cfg = tools.load_config()
    sites = "、".join(cfg.get("sites", {}).keys())
    apps = "、".join(_app_label(name, entry) for name, entry in cfg.get("apps", {}).items())
    system = "、".join(list(cfg.get("system", {}).keys()) + list(cfg.get("dangerous_system", {}).keys()))
    running = "、".join(tools.running_apps()) or "(取得なし)"
    return (
        f"【いま起動中のアプリ（現在の実状態）】: {running}\n"
        "※これは今この瞬間に開いているアプリ。これを基準に判断する。ここに無いアプリは閉じている前提。"
        "ただし Chrome の PWA（YouTube/Gmail/moodle等）はこの一覧に出ないので、開閉が不確かなら launch_app で開く"
        "（launch_app は開いていれば前面化するだけで無害）。\n"
        "【いまPCで実行できること（使うツール）】\n"
        "・サイトとアプリで同名が両方ある時はアプリ(専用ウィンドウ)を優先します。\n"
        f"・open_site（サイトを開く）の name 候補: {sites}\n"
        f"・launch_app（アプリ起動）の name 候補（カッコ内は呼び名・略語、それでも引けます）: {apps}\n"
        f"・run_system（システム操作）の name 候補: {system}\n"
        "・close_app（閉じる）: 開いたアプリ/サイトを閉じる。『〜閉じて/消して/やめて』はこれ（システム操作と混同しない）。\n"
        "・get_weather（天気）: 『天気どう？』は必ずこれで実データを取り、要約して伝える（web_searchではない）。\n"
        "・play_media（動画/音楽）: 『〜の動画見たい/流して/聴きたい』。開いて残す。\n"
        "・manage_window（ウィンドウ配置）: 『左/右に寄せて・最大化・中央・一覧』。\n"
        "・fetch_page（ページ本文取得）/ web_search（検索）/ open_url（URLを開く）: 上記に無い調べもの（株価/ニュース/事実確認等）や『調べて/教えて』に実ソースで答える。\n"
        "・アプリや検索を開くだけで終わらせず、取得した実データを根拠に答える。本当にPCで不可能なことだけ正直に断る（捏造しない）。"
    )


def build_system_prompt(persona: str) -> str:
    return persona + "\n\n" + available_operations()


MAX_HISTORY_MESSAGES = 10  # 直近5往復ぶんを保持（ADR-3：直近Nそのまま。古いものの要約は将来）
MAX_TOOL_ROUNDS = 5  # 「起動→配置」など複数ステップを許す。暴走防止に上限を設ける


def run_turn(client, persona: str, user_input: str, dry_run: bool = False, history=None) -> dict:
    """1ターン処理。{"actions": [...], "reply": "...", "history": [...]} を返す（Step5(a) 会話記憶）。

    history（直近の会話、user/assistantのテキストのみ）を前置きしてモデルに渡すことで、
    「それで行こう」「さっきの」などターンまたぎの参照が通る。返り値の history を呼び出し側が
    次ターンに渡す。履歴はテキストのみ保持し、tool_use/tool_result ブロックは持ち越さない
    （ブロックのペア不整合による API エラーを避けるため）。

    dry_run=True なら open_site / launch_app / run_system を実際には実行せず、
    どのツールが選ばれたかと返答だけを返す（公共の場での確認用）。
    """
    actions = []
    history = list(history or [])
    system_prompt = build_system_prompt(persona)  # 人格 ＋ ②材料（利用可能な操作一覧）
    messages = history + [{"role": "user", "content": user_input}]

    def _finish(reply: str) -> dict:
        new_history = (history + [
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": reply},
        ])[-MAX_HISTORY_MESSAGES:]
        # ephemeral（今ターンで開いた一時タブのURL）も返す。呼び出し側が次ターンで閉じる（ADR-0021）。
        return {"actions": actions, "reply": reply, "history": new_history,
                "ephemeral": tools.pop_ephemeral_opened()}

    # ツールが尽きる（テキストで返してくる）まで回す。これにより「起動→配置」のような
    # 複数ステップが1ターン内で完結する（旧実装は1ラウンドで打ち切り、2手目のtool_useを捨てていた）。
    response = None
    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model=MODEL, max_tokens=400, system=system_prompt, tools=tools.TOOL_DEFS, messages=messages
        )
        if response.stop_reason != "tool_use":
            return _finish(_text_of(response))

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                label = f"{block.name}({block.input})"
                if dry_run:
                    result = "（ドライラン：実際には実行していません）"
                    actions.append({"kind": "dry", "label": label, "result": result,
                                    "tool": block.name, "input": block.input})
                else:
                    result = tools.run_tool(block.name, block.input)
                    actions.append({"kind": "run", "label": label, "result": result,
                                    "tool": block.name, "input": block.input})
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": result}
                )

        # stop_reason が tool_use でも実 tool_use ブロックが無い稀ケース。空contentで
        # フォロー呼び出しすると API 400 になるため、その応答テキストで返す。
        if not tool_results:
            return _finish(_text_of(response))
        messages.append({"role": "user", "content": tool_results})

    # 上限まで回しても止まらなかった：最後にツール無しで締めの一言だけ書かせる
    final = client.messages.create(
        model=MODEL, max_tokens=300, system=system_prompt, messages=messages
    )
    reply = _text_of(final)
    if reply == "（…）" and actions:  # 実行後にモデルが何も言わなかった時の保険：ツール結果を返答に使う
        reply = actions[-1]["result"]
    return _finish(reply)
