"""MyAgent のコア処理（UIから分離）。

agent.py / web.py の両方から使える「APIキー読み込み・人格読み込み・1ターン処理」をまとめた層。
UI（ターミナル / ブラウザ）が変わっても、ここは無変更で使い回せる。
"""

from __future__ import annotations

import os
from pathlib import Path

import anthropic

import profile_store
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


def static_menu() -> str:
    """Stable part of the system prompt (cacheable): what the agent can do + config names.

    English to save tokens; config keys (sites/apps/system) stay verbatim since they
    are the actual lookup keys. Aliases are NOT listed — the resolver maps them at call
    time, so passing the user's word works regardless. Running apps live in the volatile part.
    """
    cfg = tools.load_config()
    sites = "、".join(cfg.get("sites", {}).keys())
    apps = "、".join(cfg.get("apps", {}).keys())
    system = "、".join(list(cfg.get("system", {}).keys()) + list(cfg.get("dangerous_system", {}).keys()))
    return (
        "[Capabilities]\n"
        "- If a name is both a site and an app, prefer the app (dedicated window).\n"
        "- If intent maps to a registered op, do it; if ambiguous between several, ask one short question.\n"
        "- Deliver real info: weather/temp/rain -> get_weather; article/page content -> fetch_page; "
        "other lookups (stocks/news/facts) or 調べて/教えて -> web_search/open_url. Never invent; cite the fetched data.\n"
        "- Media (動画見たい/流して/聴きたい) -> play_media (stays open). Close (閉じて/消して/やめて) -> close_app. "
        "Window (左/右/最大化/中央) -> manage_window.\n"
        "- Remember/schedule/forget per the hard rules above.\n"
        f"- open_site names: {sites}\n"
        f"- launch_app names (also openable by alias/reading; resolver handles it): {apps}\n"
        f"- run_system names: {system}"
    )


def volatile_context() -> str:
    """Per-turn changing part (not cached): current time/profile/schedule + running apps."""
    running = "、".join(tools.running_apps()) or "(none)"
    return (
        profile_store.context_text() + "\n"
        f"[Running apps now]: {running}\n"
        "These are the apps open right now — judge from this. Not listed = closed. "
        "Chrome PWAs (YouTube/Gmail/moodle) don't appear here, so if unsure just launch_app "
        "(harmless — only brings to front if already open)."
    )


_RULES = (
    "[Hard rules — top priority]\n"
    "1. A request to save a schedule (毎週〜 / 来週の◯曜 / a dated event) MUST call add_schedule. "
    "A request to remember personal info (覚えておいて etc.) MUST call remember. "
    "NEVER say you saved/registered/remembered something without first calling the tool — that would be a lie (nothing is stored). "
    "Only use save/registered wording after the tool has run.\n"
    "2. Only report actions you actually performed (tool results). Never claim a launch/placement/save you did not do.\n"
    "3. Always reply to the user in Japanese, fully in character (see persona below)."
)


def build_system_prompt(persona: str):
    """System prompt as 2 blocks for prompt caching (コスト最適化):
    - stable block (rules + persona + static menu) carries cache_control -> also caches
      the tools (rendered before system). Reused across the multi-round loop & bursts at ~0.1x.
    - volatile block (current time / profile / running apps) sits after the breakpoint, uncached.
    """
    stable = _RULES + "\n\n" + persona + "\n\n" + static_menu()
    return [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": volatile_context()},
    ]


MAX_HISTORY_MESSAGES = 6  # 直近3往復（コスト最適化で10→6。古いものの要約は将来）
MAX_TOOL_ROUNDS = 5  # 「起動→配置」など複数ステップを許す。暴走防止に上限を設ける

# 捏造ガード（ADR-0030）: 保存ツール／保存"完了"を断定する言い回し
_SAVE_TOOLS = {"remember", "add_schedule", "forget"}
_SAVE_CLAIMS = ("覚えました", "覚えておきました", "記憶しました", "登録しました", "登録完了",
                "保存しました", "メモしました", "セットしました", "記録しました")


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

    def _force_save(claim_reply: str) -> "str | None":
        """「保存しました」と言ったのにツールを呼んでいない時、ツールを強制して本当に保存させる。"""
        msgs = history + [
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": claim_reply},
            {"role": "user", "content": "（システム指示）いま保存・登録したと言いましたが、まだツールを実行していません。"
             "remember / add_schedule / forget のうち適切なものを今すぐ呼んで、実際に保存してください。"},
        ]
        try:
            r = client.messages.create(model=MODEL, max_tokens=300, system=system_prompt,
                                       tools=tools.TOOL_DEFS, tool_choice={"type": "any"}, messages=msgs)
        except Exception:
            return None
        saved = None
        for block in r.content:
            if block.type == "tool_use":
                result = tools.run_tool(block.name, block.input)
                actions.append({"kind": "run", "label": f"{block.name}({block.input})",
                                "result": result, "tool": block.name, "input": block.input})
                saved = result
        return saved

    def _rephrase(result: str) -> str:
        """ツール結果を人格・口調で一言に言い換える（無言フォールバックでロボ口調が漏れるのを防ぐ）。"""
        try:
            r = client.messages.create(
                model=MODEL, max_tokens=150, system=system_prompt,
                messages=[
                    {"role": "user", "content": user_input},
                    {"role": "user", "content": f"（システム）今の操作の結果はこうです:「{result}」。"
                     "事実は変えずに、これをあなたの口調・人格でマスターへ一言だけ伝えてください。"},
                ],
            )
            t = _text_of(r)
            return t if t != "（…）" else result
        except Exception:
            return result

    def _finish(reply: str) -> dict:
        if reply == "（…）" and actions:  # ツール実行後にモデルが無言だった時の保険
            # 実モードは素のツール結果でなく人格で言い換える（dry_runは検証用に素のまま）
            reply = actions[-1]["result"] if dry_run else _rephrase(actions[-1]["result"])
        # 捏造ガード：保存完了を口にしたのに保存ツールを呼んでいない → 強制実行して本当に保存する（ADR-0030）
        if (not dry_run and not any(a["tool"] in _SAVE_TOOLS for a in actions)
                and any(p in reply for p in _SAVE_CLAIMS)):
            forced = _force_save(reply)
            if forced:
                reply = forced
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
