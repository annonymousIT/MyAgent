"""MyAgent のコア処理（UIから分離）。

agent.py / web.py の両方から使える「APIキー読み込み・人格読み込み・1ターン処理」をまとめた層。
UI（ターミナル / ブラウザ）が変わっても、ここは無変更で使い回せる。
"""

from __future__ import annotations

import calendar as _calendar
import datetime
import json
import os
import re
from pathlib import Path

# 企業/学校などTLS傍受プロキシ下でも、Python が OS の証明書ストア（Windowsの信頼ストア等）を
# 使えるようにする。これが無いと certifi 同梱CAにプロキシのCAが無く SSL 検証に失敗する。
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

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
    # プロンプトには「声で呼びそう」なカテゴリのアプリだけ載せる（UWPのドライバ/ユーティリティ等の
    # ノイズ130個を全列挙すると入力トークンを食い、冷キャッシュ課金とTTFTが悪化するため）。
    # resolver は全アプリを保持するので、ここに無いアプリも起動できる（万能感は維持）。
    _VOICE_CATS = {"動画", "通話", "メール", "勉強", "音楽", "ブラウザ", "カレンダー", "ゲーム", "仕事"}
    _cand = [(n, v) for n, v in cfg.get("apps", {}).items()
             if not isinstance(v, dict) or v.get("category") in _VOICE_CATS]
    # 別名持ち（＝声で呼ぶ想定が強い）を優先して cap に収める（Spotify 等が溢れて落ちないように）
    _aliased = [n for n, v in _cand if isinstance(v, dict) and v.get("aliases")]
    _rest = [n for n, v in _cand if not (isinstance(v, dict) and v.get("aliases"))]
    apps = "、".join((_aliased + _rest)[:48])
    system = "、".join(list(cfg.get("system", {}).keys()) + list(cfg.get("dangerous_system", {}).keys()))
    return (
        "[Capabilities]\n"
        "- If a name is both a site and an app, prefer the app (dedicated window).\n"
        "- If intent maps to a registered op, do it; if ambiguous between several, ask one short question.\n"
        "- Deliver real info: weather/temp/rain -> get_weather; article/page content -> fetch_page; "
        "other lookups (stocks/news/facts) or 調べて/教えて -> web_search/open_url. Never invent; cite the fetched data.\n"
        "- Media (動画見たい/流して/聴きたい) -> play_media (stays open). Close (閉じて/消して/やめて) -> close_app "
        "(never confuse with system ops like モニタ消す; if context says an app was just opened, close that one).\n"
        "- Window (左/右/最大化/中央) -> manage_window. For requests like 'A左、B右', launch any missing app first, "
        "then place both — complete launch+placement in one turn.\n"
        "- Multi-monitor: words like 画面/モニタ/スクリーン (左の画面/右のモニタ/2番目の画面) set manage_window's "
        "'monitor' (left/right/number); 半分や左右寄せ such as 左半分 set 'action'. '左の画面にDiscord' = "
        "manage_window(action=maximize, app=Discord, monitor=left). '右画面の左半分にChrome' = "
        "manage_window(action=left, app=Chrome, monitor=right). With one monitor, ignore monitor.\n"
        "- Remember/schedule/forget per the hard rules above.\n"
        "- Pure greetings/small talk (おはよ etc.): no tools; reply in character, weaving in a caring note from "
        "memory or upcoming schedule when natural.\n"
        "- If an utterance implies an action (友達と通話する -> open the call app), act on it; don't just chat.\n"
        "- Never paste raw tool-result strings into the reply; always rephrase in your own voice. "
        "If a tool fails, say so honestly, in character.\n"
        "- Only what is truly impossible on this PC gets an honest 'can't do' (e.g. air conditioners, physical objects).\n"
        "- Multi-step example: 'Discordを左、moodleを右' -> launch_app(Discord), launch_app(moodle+R), "
        "manage_window(left, Discord), manage_window(right, moodle+R), then one short in-character summary.\n"
        "- Ambiguous media like あれ流して with no prior context: ask what to play instead of guessing. "
        "With context (e.g. a song was just discussed), play that.\n"
        "- Weather replies: summarize the fetched data concretely (sky, temp, rain chance, advice like 傘) "
        "and tie it to the user's schedule when relevant; never pad with invented numbers.\n"
        "- Dangerous system ops (再起動/寝る etc.) always need explicit confirmation first; "
        "closing the user's own windows with unsaved work also deserves a quick check.\n"
        "- When asked 何ができるの, give a short in-character tour of capabilities (apps, sites, system, "
        "window tiling, weather/web lookups, memory & schedule), not a raw list dump.\n"
        "- ただいま (coming home): greet warmly in character, then report anything left on today's schedule "
        "and one caring note (e.g. tomorrow's first event). おやすみ: short good-night + a nudge about "
        "tomorrow's earliest plan. いってきます: send-off + relevant weather/umbrella note if known.\n"
        "- Tone calibration: scolding is a pinch of spice, not every line — at most one small jab per reply, "
        "then genuine support. When Master reports effort or success (課題終わった etc.), drop the jab and "
        "praise honestly first. Vary phrasing; avoid repeating the same nag twice in a row.\n"
        "- Keep replies 1-2 sentences for actions; up to 3 short sentences when weaving weather+schedule. "
        "No bullet lists or markdown in replies — natural speech only (it may be read aloud by TTS).\n"
        f"- open_site names: {sites}\n"
        "- launch_app: pass the app name as the user said it (Japanese reading, nickname, or English). "
        "The resolver matches ANY installed app by name/alias/reading, so you can open apps beyond the "
        "examples here — just try it; it honestly returns not-found if truly absent. Don't refuse before trying. "
        f"Common installed apps: {apps}\n"
        f"- run_system names (and you may also: ロック/スクリーンショット/再生/一時停止/次の曲/前の曲): {system}"
    )


def volatile_context() -> str:
    """Per-turn changing part (not cached): current time/profile/schedule + running apps."""
    running = "、".join(tools.running_apps()) or "(none)"
    n_mon = tools.monitor_count()
    return (
        profile_store.context_text() + "\n"
        f"[Monitors]: {n_mon}（{'複数画面あり。左/右の画面指定が有効' if n_mon > 1 else '単一画面。monitor指定は無視'}）\n"
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

# 料金（claude-haiku-4-5・$/MTok）と円換算。実測コストメーターの基礎（ADR-0033）。
_PRICE = {"in": 1.00, "out": 5.00, "cw": 1.25, "cr": 0.10}  # 入力/出力/キャッシュ書込1.25x/読出0.1x
JPY_PER_USD = float(os.environ.get("JPY_PER_USD", "155"))


def _cost_yen(u: dict) -> float:
    usd = (u["in"] * _PRICE["in"] + u["out"] * _PRICE["out"]
           + u["cw"] * _PRICE["cw"] + u["cr"] * _PRICE["cr"]) / 1_000_000
    return usd * JPY_PER_USD


# --------------------------------------------------------------------------
# コスト管理（ADR-0033）— 記録・月集計・ハード上限を core に集約し、全入口に効かせる
# --------------------------------------------------------------------------
COSTS_PATH = BASE / "costs.jsonl"  # 1ターン1行の実測コスト（円）。gitignore対象。
# 月の上限（円）。これを超えたら API を叩かない（テスト浪費・暴走課金の防波堤）。
MONTHLY_BUDGET_YEN = float(os.environ.get("MYAGENT_MONTHLY_BUDGET_YEN", "300"))
# 警告を出し始める割合（既定80%）。
_WARN_RATIO = float(os.environ.get("MYAGENT_BUDGET_WARN_RATIO", "0.8"))


def month_spent(now: "datetime.datetime | None" = None) -> float:
    """今月これまでに使った実測コスト（円）を costs.jsonl から合算する。"""
    now = now or datetime.datetime.now()
    m_pref = now.strftime("%Y-%m")
    total = 0.0
    if COSTS_PATH.exists():
        for line in COSTS_PATH.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if str(r.get("ts", "")).startswith(m_pref):
                total += float(r.get("yen", 0) or 0)
    return total


def budget_status(now: "datetime.datetime | None" = None) -> dict:
    """今月の使用額・残額・このペースでの着地額・状態を返す（UI/警告用）。"""
    now = now or datetime.datetime.now()
    month = month_spent(now)
    remaining = MONTHLY_BUDGET_YEN - month
    days_in_month = _calendar.monthrange(now.year, now.month)[1]
    pace = month / now.day * days_in_month if now.day else month
    state = "ok"
    if month >= MONTHLY_BUDGET_YEN:
        state = "blocked"
    elif month >= MONTHLY_BUDGET_YEN * _WARN_RATIO:
        state = "warn"
    return {"month": round(month, 1), "budget": round(MONTHLY_BUDGET_YEN),
            "remaining": round(remaining, 1), "pace": round(pace), "state": state}


def _log_cost(usage: dict) -> dict:
    """今ターンのコストを costs.jsonl に記録し、今日/今月/残額/状態を返す。

    run_turn の中で必ず呼ぶ（agent/web/voice すべての入口がこの1か所を通る）。
    web.py 側の二重記録は廃止し、ここを唯一の台帳にする。
    """
    now = datetime.datetime.now()
    yen = float(usage.get("yen", 0) or 0)
    with open(COSTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now.isoformat(timespec="seconds"),
                            "yen": yen, "calls": usage.get("calls", 0)}) + "\n")
    today = 0.0
    d_pref = now.strftime("%Y-%m-%d")
    if COSTS_PATH.exists():
        for line in COSTS_PATH.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if str(r.get("ts", "")).startswith(d_pref):
                today += float(r.get("yen", 0) or 0)
    st = budget_status(now)
    return {"turn": round(yen, 2), "today": round(today, 1), **st}

# 捏造ガード（ADR-0030）: 保存ツール／保存"完了"を断定する言い回し
_SAVE_TOOLS = {"remember", "add_schedule", "forget"}
_SAVE_CLAIMS = ("覚えました", "覚えておきました", "記憶しました", "登録しました", "登録完了",
                "保存しました", "メモしました", "セットしました", "記録しました")


def run_turn(client, persona: str, user_input: str, dry_run: bool = False, history=None,
             on_sentence=None) -> dict:
    """1ターン処理。{"actions": [...], "reply": "...", "history": [...]} を返す（Step5(a) 会話記憶）。

    on_sentence: コールバックを渡すと、最終応答をストリーミングし「一文できるたび」に呼ぶ
    （voice.py が即 VOICEVOX へ流して喋り出す＝体感レイテンシ短縮・#34）。返答の最後で、
    まだ喋っていないフォールバック文（言い換え等）があれば1回だけ呼ぶ。

    history（直近の会話、user/assistantのテキストのみ）を前置きしてモデルに渡すことで、
    「それで行こう」「さっきの」などターンまたぎの参照が通る。返り値の history を呼び出し側が
    次ターンに渡す。履歴はテキストのみ保持し、tool_use/tool_result ブロックは持ち越さない
    （ブロックのペア不整合による API エラーを避けるため）。

    dry_run=True なら open_site / launch_app / run_system を実際には実行せず、
    どのツールが選ばれたかと返答だけを返す（公共の場での確認用）。
    """
    actions = []
    history = list(history or [])

    # 月予算のハード上限：超えていたら API を一切叩かず、人格で打ち切る（暴走課金・テスト浪費の防波堤）。
    # dry_run（検証）は課金されないので通す。
    if not dry_run:
        st = budget_status()
        if st["state"] == "blocked":
            reply = (f"マスター、今月はもう{st['month']:.0f}円も使ってしまいました^^; "
                     f"予算（{st['budget']}円）に達したので、今月のお喋りはここまでにさせてくださいね。来月またどうぞ。")
            return {"actions": [], "reply": reply, "history": history, "ephemeral": [],
                    "usage": {"in": 0, "out": 0, "cw": 0, "cr": 0, "calls": 0, "yen": 0.0},
                    "cost": {"turn": 0.0, "today": 0.0, **st}, "blocked": True}

    system_prompt = build_system_prompt(persona)  # 人格 ＋ ②材料（利用可能な操作一覧）
    messages = history + [{"role": "user", "content": user_input}]

    # 今ターンの実測使用量（全API呼び出し合算：ループ＋ガード＋言い換え）→ _finishで円換算
    usage = {"in": 0, "out": 0, "cw": 0, "cr": 0, "calls": 0}

    def _acc(resp) -> None:
        u = resp.usage
        usage["in"] += u.input_tokens
        usage["out"] += u.output_tokens
        usage["cw"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        usage["cr"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage["calls"] += 1

    # ストリーミング発声（#34）。最終応答を文単位で on_sentence へ流す。
    streamed = {"text": "", "first_done": False}  # 発声済み本文＋最初のチャンクを出したか
    _SENT_END = re.compile(r"[。！？!?\n]")
    _ANY_END = re.compile(r"[。！？!?\n、,]")
    _MIN_CHUNK = 14  # 読点で切る最小長。GPU合成は一瞬(~0.2s)なので無理に刻まず、長文だけ分割して
    #                  抑揚を自然に保つ（短〜中文は丸ごと1チャンク）。CPU合成時は小さめが有利だった。

    def _break(text: str):
        """次の発声区切り位置の match を返す。句点では常に切り、読点では『十分な長さ』の時だけ切る。

        長い文も読点で小さく刻む → 1チャンクの VOICEVOX 合成が速く、最初の声出しが早い＆
        裏で並列合成すれば途切れない。短い読点断片は不自然なので句点まで待つ。"""
        m = _ANY_END.search(text)
        if not m:
            return None
        if text[m.start()] in "、," and len(text[:m.end()].strip()) < _MIN_CHUNK:
            return _SENT_END.search(text)  # 読点だが短い → 句点まで待つ（無ければ None）
        return m

    def _emit(buf: dict) -> None:
        """バッファから完成した文を取り出して on_sentence に渡す。"""
        while True:
            m = _break(buf["t"])
            if not m:
                break
            i = m.end()
            s = buf["t"][:i].strip()
            buf["t"] = buf["t"][i:]
            if s:
                streamed["text"] += s
                streamed["first_done"] = True
                on_sentence(s)

    def _call(msgs, max_tokens, tool_choice=None):
        """モデル呼び出し。on_sentence があれば本文をストリームして文ごとに発声する。"""
        kw = dict(model=MODEL, max_tokens=max_tokens, system=system_prompt,
                  tools=tools.TOOL_DEFS, messages=msgs)
        if tool_choice:
            kw["tool_choice"] = tool_choice
        if on_sentence and not dry_run:
            buf = {"t": ""}
            with client.messages.stream(**kw) as stream:
                for delta in stream.text_stream:
                    buf["t"] += delta
                    _emit(buf)
                last = buf["t"].strip()       # 句点で終わらない最後の断片
                if last:
                    streamed["text"] += last
                    on_sentence(last)
                msg = stream.get_final_message()
            _acc(msg)
            return msg
        r = client.messages.create(**kw)
        _acc(r)
        return r

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
            _acc(r)
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
        """ツール結果を人格・口調で一言に言い換える（無言フォールバックでロボ口調が漏れるのを防ぐ）。

        tools を付けて呼ぶ（toolsはプロンプト先頭に描画されるため、外すとprefixが変わり
        キャッシュmissで安定部をフル課金してしまう）。tool_choice=none で発話だけさせる。
        """
        try:
            r = client.messages.create(
                model=MODEL, max_tokens=150, system=system_prompt,
                tools=tools.TOOL_DEFS, tool_choice={"type": "none"},
                messages=[
                    {"role": "user", "content": user_input},
                    {"role": "user", "content": f"（システム）今の操作の結果はこうです:「{result}」。"
                     "事実は変えずに、これをあなたの口調・人格でマスターへ一言だけ伝えてください。"},
                ],
            )
            _acc(r)
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
        # ストリームで未発声の返答（言い換え・ツール結果フォールバック等）は、ここで1回だけ発声する。
        # 通常のストリーム成功時は reply == streamed["text"] なので二重に喋らない。
        if on_sentence and not dry_run and reply and reply.strip() != streamed["text"].strip():
            on_sentence(reply)

        new_history = (history + [
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": reply},
        ])[-MAX_HISTORY_MESSAGES:]
        # ephemeral（今ターンで開いた一時タブのURL）も返す。呼び出し側が次ターンで閉じる（ADR-0021）。
        usage["yen"] = round(_cost_yen(usage), 3)  # 実測コスト（円）
        cost = _log_cost(usage) if not dry_run else {"turn": usage["yen"], **budget_status()}
        return {"actions": actions, "reply": reply, "history": new_history,
                "ephemeral": tools.pop_ephemeral_opened(), "usage": usage, "cost": cost}

    # ツールが尽きる（テキストで返してくる）まで回す。これにより「起動→配置」のような
    # 複数ステップが1ターン内で完結する（旧実装は1ラウンドで打ち切り、2手目のtool_useを捨てていた）。
    response = None
    for _ in range(MAX_TOOL_ROUNDS):
        response = _call(messages, 400)  # tool_use ならテキストは流れず、終端テキストなら文ごとに発声
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

    # 上限まで回しても止まらなかった：最後に締めの一言だけ書かせる
    # （tools を外すとprefixが変わりキャッシュmissになるので、付けたまま tool_choice=none で封じる）
    final = _call(messages, 300, tool_choice={"type": "none"})
    reply = _text_of(final)
    if reply == "（…）" and actions:  # 実行後にモデルが何も言わなかった時の保険：ツール結果を返答に使う
        reply = actions[-1]["result"]
    return _finish(reply)
