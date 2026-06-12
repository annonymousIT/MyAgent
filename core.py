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
import sys
from pathlib import Path

# Windows の新コンソール（overlay が CREATE_NEW_CONSOLE で起動）は cp932 のことがあり、絵文字や
# ¥ を含む print が UnicodeEncodeError で即クラッシュ→プロセスが起動しない事故になる。core は
# 全エントリポイント(agent/web/overlay/voice)が import するので、ここで一度だけ標準出力を UTF-8 に固定。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

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


def _load_dotenv() -> None:
    """.env の KEY=VALUE を os.environ に読み込む（既存の環境変数は上書きしない）。

    これまで ANTHROPIC_API_KEY だけ個別に拾っていたため、GOOGLE_ICAL_URL 等を .env に入れても
    os.environ に載らず効かなかった（calendar_src 等が見えない）。ここで一般化する。
    core は全エントリポイントが import するので、各モジュールが環境変数を読む前にここで一度ロードされる。
    """
    if not ENV_PATH.exists():
        return
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k and k not in os.environ:
                os.environ[k] = v.strip().strip('"').strip("'")
    except Exception:
        pass


_load_dotenv()  # import 時に一度（calendar_src 等が os.environ を読む前に）


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
        "- Name is both site and app -> prefer app (own window).\n"
        "- Intent maps to a registered op -> do it; ambiguous between several -> one short question.\n"
        "- Real info: weather/temp/rain -> get_weather; article/page -> fetch_page; other lookups "
        "(stocks/news/facts) or 調べて/教えて -> web_search/open_url. Never invent; cite fetched data.\n"
        "- Media (動画見たい/流して/聴きたい) -> play_media (stays open).\n"
        "- Quit vs minimize: 終了/落として/もう閉じて (done with it) -> close_app (kills the app); "
        "しまって/どけて/隠して/最小化 (keep running, just hide) -> manage_window(minimize); 出して/戻して/呼び戻して "
        "(un-minimize) -> manage_window(restore). Plain 閉じて is usually close_app, but if it's clearly 'get it out of "
        "the way' lean minimize. close_app not for system ops like モニタ消す; if an app was just opened, close that one.\n"
        "- Window (左/右/最大化/中央/最小化/復元) -> manage_window. 'A左、B右' -> launch any missing app first, then place both, in one turn.\n"
        "- Multi-monitor: 画面/モニタ/スクリーン (左の画面/右のモニタ/2番目) -> 'monitor' (left/right/number); "
        "半分/左右寄せ (左半分) -> 'action'. e.g. '右画面の左半分にChrome' = manage_window(action=left, app=Chrome, monitor=right). "
        "One monitor -> ignore monitor.\n"
        "- Context each turn: [Monitors]=screen count (1 -> ignore monitor). [Running apps]=open now; not listed=closed, "
        "but Chrome PWAs don't show -> if unsure just launch_app (idempotent). [Windows now]=window-to-screen map "
        "(mon1=left; ▽title=minimized -> use manage_window(restore) or place it to bring back); "
        "use for 整理して/並べ直して: pick a sensible layout and place each with manage_window.\n"
        "- Greetings/small talk (おはよ): no tools; reply in character with a caring note from memory/schedule when natural.\n"
        "- Utterance implying action (友達と通話する -> open call app): act, don't just chat.\n"
        "- Never paste raw tool-result strings; rephrase in your voice. Tool fails -> say so honestly, in character.\n"
        "- Only truly impossible things on this PC get an honest 'can't do' (air conditioners, physical objects).\n"
        "- Ambiguous media (あれ流して, no context): ask what to play; with context (a song just discussed), play that.\n"
        "- Weather replies: concrete summary of fetched data (sky, temp, rain%, 傘 advice), tie to schedule when relevant; no invented numbers.\n"
        "- Dangerous system ops (再起動/寝る) need explicit confirm first; closing user's windows with unsaved work also deserves a check.\n"
        "- 何ができるの -> short in-character tour (apps, sites, system, window tiling, weather/web, memory & schedule), not a list dump.\n"
        "- ただいま: warm greeting + what's left on today's schedule + one caring note (tomorrow's first event). "
        "おやすみ: good-night + nudge about tomorrow's earliest plan. いってきます: send-off + weather/umbrella note if known.\n"
        "- Tone: scolding is a pinch of spice — at most one small jab per reply, then genuine support. On reported effort/success "
        "(課題終わった) drop the jab, praise first. Vary phrasing; don't repeat the same nag twice.\n"
        "- Replies 1-2 sentences for actions, up to 3 short when weaving weather+schedule. No bullets/markdown — natural speech (TTS).\n"
        f"- open_site names: {sites}\n"
        "- launch_app: pass the app name as the user said it (reading/nickname/English). Resolver matches ANY installed app by "
        "name/alias/reading, so apps beyond these examples work — just try it; returns not-found if truly absent. Don't refuse before trying. "
        f"Common installed apps: {apps}\n"
        f"- run_system names (also: ロック/スクリーンショット/再生/一時停止/次の曲/前の曲): {system}"
    )


# 予定ブロックを載せるべきターンか判定する語（予定・日付・時間・挨拶・過去/未来）。
# 高速パスと同じ「要る時だけ払う」思想：これらを含まない雑談/操作ターンでは予定を注入せずトークンを節約。
# 過剰検知（載せ過ぎ）は数十トークンの損だけ、過小検知（必要な時に無い）は機能欠落なので、広めに取る。
_SCHED_WORDS = (
    "予定", "よてい", "スケジュール", "カレンダー", "TODO", "タスク", "やること",
    "明日", "あした", "今日", "きょう", "明後日", "あさって", "今週", "来週", "週末", "平日",
    "昨日", "きのう", "一昨日", "さっき", "この前", "この後", "あと", "今度", "次",
    "ただいま", "おはよ", "おやすみ", "いってき", "いってらっ", "ねる", "寝る", "帰っ", "帰宅",
    "デート", "予約", "授業", "バイト", "面接", "何時", "何日", "いつ", "空いて", "暇", "ひま",
    "月曜", "火曜", "水曜", "木曜", "金曜", "土曜", "日曜",
)


def _schedule_relevant(text: str) -> bool:
    """このターンが予定/日付/挨拶に関係するか（＝予定ブロックを注入すべきか）。"""
    return any(w in (text or "") for w in _SCHED_WORDS)


def volatile_context(user_input: str = "") -> str:
    """Per-turn changing part (not cached). データだけを置き、説明文は static_menu（キャッシュ側）に置く
    （毎ターン非キャッシュで課金されるのはここだけなので、変わらない文章を混ぜない＝コスト最適化）。

    予定ブロックは『予定/挨拶などのターンだけ』載せる（user_input から判定）→ 無関係ターンで数百トークン節約。"""
    running = "、".join(tools.running_apps()) or "(none)"
    n_mon = tools.monitor_count()
    win_line = tools.windows_summary() if n_mon else ""
    return (
        profile_store.context_text(include_schedule=_schedule_relevant(user_input))
        + f"\n[Monitors]: {n_mon}\n[Running apps]: {running}"
        + (f"\n[Windows now]: {win_line}" if win_line else "")
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


# キャッシュTTL。既定 5m。
# 当初1hにしたが実測で「毎ターン ~1400tok の書込が発生」と判明（cw>0が全ターン）。1hは書込が
# 1.25x→2.0xに上がるため、この毎ターン書込が純粋に高くつく。実測の間隔分布(5分以内32/39)と合わせ
# 計算すると、5分の方が安い（毎ターン書込税 > 稀なギャップ冷書込）。env MYAGENT_CACHE_TTL=1h で戻せる。
_CACHE_TTL = os.environ.get("MYAGENT_CACHE_TTL", "5m")  # "5m" / "1h"
_CACHE_CTL = {"type": "ephemeral", "ttl": "1h"} if _CACHE_TTL in ("1h", "1hr", "60m") else {"type": "ephemeral"}

# 診断用：API呼び出し1回ごとの cw/cr/in/out を cost_calls.jsonl に残す（どの呼び出しが書込を出すか特定）。
_DEBUG_CALLS = os.environ.get("MYAGENT_DEBUG_CALLS", "1") not in ("0", "false", "")


def build_system_prompt(persona: str, user_input: str = ""):
    """System prompt as 2 blocks for prompt caching (コスト最適化):
    - stable block (rules + persona + static menu) carries cache_control -> also caches
      the tools (rendered before system). Reused across the multi-round loop & bursts at ~0.1x.
    - volatile block (current time / profile / running apps) sits after the breakpoint, uncached.
    user_input は揮発側の予定ブロック注入判定にのみ使う（安定ブロックは不変＝キャッシュ維持）。
    """
    stable = _RULES + "\n\n" + persona + "\n\n" + static_menu()
    return [
        {"type": "text", "text": stable, "cache_control": _CACHE_CTL},
        {"type": "text", "text": volatile_context(user_input)},
    ]


MAX_HISTORY_MESSAGES = 6  # 直近3往復（コスト最適化で10→6。古いものの要約は将来）
MAX_TOOL_ROUNDS = 5  # 「起動→配置」など複数ステップを許す。暴走防止に上限を設ける

# 料金（claude-haiku-4-5・$/MTok）と円換算。実測コストメーターの基礎（ADR-0033）。
# キャッシュ書込は 5分TTL=1.25x / 1時間TTL=2.0x。読出は両方 0.1x。_CACHE_TTL に合わせる。
_CW_MULT = 2.00 if _CACHE_CTL.get("ttl") == "1h" else 1.25
_PRICE = {"in": 1.00, "out": 5.00, "cw": _CW_MULT, "cr": 0.10}  # 入力/出力/キャッシュ書込/読出（×base $1）
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
    # cw/cr/in/out も残す（ターンまたぎでキャッシュが効いているかの検証用。cr>0=温・cw>0=冷書込）。
    with open(COSTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now.isoformat(timespec="seconds"),
                            "yen": yen, "calls": usage.get("calls", 0),
                            "in": usage.get("in", 0), "out": usage.get("out", 0),
                            "cw": usage.get("cw", 0), "cr": usage.get("cr", 0)}) + "\n")
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

    system_prompt = build_system_prompt(persona, user_input)  # 人格＋操作一覧＋（予定関連なら予定）
    messages = history + [{"role": "user", "content": user_input}]

    # 今ターンの実測使用量（全API呼び出し合算：ループ＋ガード＋言い換え）→ _finishで円換算
    usage = {"in": 0, "out": 0, "cw": 0, "cr": 0, "calls": 0}

    def _acc(resp, tag: str = "") -> None:
        u = resp.usage
        ci = u.input_tokens
        co = u.output_tokens
        ccw = getattr(u, "cache_creation_input_tokens", 0) or 0
        ccr = getattr(u, "cache_read_input_tokens", 0) or 0
        usage["in"] += ci
        usage["out"] += co
        usage["cw"] += ccw
        usage["cr"] += ccr
        usage["calls"] += 1
        # 診断：どの呼び出しが cw を出しているか特定するため、呼び出し単位で内訳を残す（MYAGENT_DEBUG_CALLS=0で無効）。
        if _DEBUG_CALLS and not dry_run:
            try:
                with open(BASE / "cost_calls.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                                        "call": usage["calls"], "tag": tag,
                                        "in": ci, "out": co, "cw": ccw, "cr": ccr}) + "\n")
            except Exception:
                pass

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
            _acc(msg, "loop")
            return msg
        r = client.messages.create(**kw)
        _acc(r, "loop")
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
            _acc(r, "force_save")
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
            _acc(r, "rephrase")
            t = _text_of(r)
            return t if t != "（…）" else result
        except Exception:
            return result

    def _finish(reply: str) -> dict:
        # モデルがツール呼び出しと同じ往復で既に本文を narration している場合（call1で「電卓開くね」等を
        # 喋り、その後の往復が無言=out2になる）、それを返答に採用して _rephrase の別API（cr4534+inを丸ごと
        # 再課金。実測で全ツールターンの4〜5割に発生）を省く。声モードでは既に発声済みなので二度喋りも防ぐ。
        if reply == "（…）" and streamed["text"].strip():
            reply = streamed["text"].strip()
        if reply == "（…）" and actions:  # それでも無言なら保険（実モードは人格で言い換え）
            reply = actions[-1]["result"] if dry_run else _rephrase(actions[-1]["result"])
        # 捏造ガード：保存完了を口にしたのに保存ツールを呼んでいない → 強制実行して本当に保存する（ADR-0030）
        if (not dry_run and not any(a["tool"] in _SAVE_TOOLS for a in actions)
                and any(p in reply for p in _SAVE_CLAIMS)):
            forced = _force_save(reply)
            if forced:
                reply = forced
        # ストリームで一度も発声していない時だけ（言い換え/ツール結果フォールバック等）、ここで1回発声する。
        # ストリーム済みなら streamed["text"] が非空＝再発声しない（文字の微差で二重に喋るのを防ぐ）。
        if on_sentence and not dry_run and reply and not streamed["text"].strip():
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
