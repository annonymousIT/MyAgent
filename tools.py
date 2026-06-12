"""操作（“手”）の実装。OS（Windows / Mac）を自動判定し、対応する設定表から実体を引く。

設定は2層（ADR-0011）:
  - config.auto.json … OSスキャンで自動生成（apps：起動対象・分類・日本語エイリアス）。再生成可。
  - config.user.json … 手動。sites（URL）/ system / dangerous_system / apps（手動追加）/ overrides。
load_config() が両者をマージし、**user層が auto層を上書き**する。
2層ファイルが無ければ従来の config_mac.json / config_win.json にフォールバック（後方互換）。

サイトとアプリで名前が衝突したときは **アプリ(PWA)優先**（ADR-0016）。
config.user.json の "overrides" に {"<name>": "site"} を置くと、その name だけサイト優先に戻せる。
"""

from __future__ import annotations

import html
import json
import os
import platform
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import profile_store

IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"

BASE = Path(__file__).parent

# 2層（新）
AUTO_CONFIG_PATH = BASE / "config.auto.json"
USER_CONFIG_PATH = BASE / "config.user.json"

# 旧・単一config（後方互換フォールバック）
_LEGACY_NAME = "config_mac.json" if IS_MAC else "config_win.json"
LEGACY_CONFIG_PATH = BASE / _LEGACY_NAME

# エラーメッセージ等で「どこを直せばいいか」を案内するための参照名
CONFIG_HINT = "config.user.json" if (AUTO_CONFIG_PATH.exists() or USER_CONFIG_PATH.exists()) else _LEGACY_NAME


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _open_in_browser(url: str) -> None:
    """URLを開く（全open系の共通経路）。Windows では CDP（タブ管理できる Chrome）を優先する。

    優先順位（ADR-0040）: ①CDPが生きてる→CDPでタブを開く（後で確実に閉じられる）
    ②Chrome未起動→デバッグポート付きで起動（次回からタブ管理可能に）③それ以外→既定ブラウザ
    （既にフラグ無しChromeが居る場合。タブの後片付けは不可＝従来どおり）。
    """
    if IS_WINDOWS:
        try:
            import cdp
            import win_ops
            if cdp.available() and cdp.open_tab(url):
                return
            if not win_ops.app_is_running("chrome.exe") and cdp.launch_chrome_with_cdp(url):
                return
        except Exception:
            pass
    webbrowser.open(url)


def load_config() -> dict:
    """2層（auto + user）をマージして返す。user が auto を上書き。

    2層ファイルがどちらか存在すればそれを使い、無ければ旧 config_*.json に
    フォールバックする（後方互換）。

    返す構造（マージ後）:
      {
        "sites": {...},              # user層のみ（URLは手動必須）
        "apps":  {                   # auto層 + user層（user優先）
          "<表示名>": {"target","kind","path","category","aliases"} | "<旧来の文字列>"
        },
        "system": {...},             # user層（無ければlegacy）
        "dangerous_system": {...},
        "overrides": {"<name>": "site"}  # サイト優先に戻す指定
      }
    """
    auto = _read_json(AUTO_CONFIG_PATH)
    user = _read_json(USER_CONFIG_PATH)

    # どちらの新ファイルも無ければ旧configにフォールバック
    if not auto and not user:
        return _read_json(LEGACY_CONFIG_PATH)

    # apps は auto を土台に user で上書き（user優先）
    merged_apps = dict(auto.get("apps", {}))
    merged_apps.update(user.get("apps", {}))

    # user の "hide" に挙げたアプリ名は候補から外す（スキャンが拾った不要アプリの抑制）。
    # 例: 標準カレンダーを隠して「カレンダー」をGoogleカレンダーに一本化する。
    for name in user.get("hide", []):
        merged_apps.pop(name, None)

    # user の "aliases"（あだ名→アプリ表示名）を、既存アプリの aliases に注入する。
    # 例: {"ばろ": "VALORANT"} で「ばろ」が VALORANT を引けるようになる（エントリ全体を複製せず追加だけ）。
    for nick, appname in user.get("aliases", {}).items():
        tgt = _nfc(str(appname)).lower()
        for key, entry in merged_apps.items():
            if isinstance(entry, dict) and _nfc(key).lower() == tgt:
                al = list(entry.get("aliases") or [])
                if nick not in al:
                    al.append(nick)
                entry["aliases"] = al
                break

    return {
        "sites": user.get("sites", {}),
        "apps": merged_apps,
        "system": user.get("system", {}),
        "dangerous_system": user.get("dangerous_system", {}),
        "overrides": user.get("overrides", {}),
    }


# --------------------------------------------------------------------------
# apps の解決（表示名 + エイリアスで引く）
# --------------------------------------------------------------------------
def _app_target(entry) -> "str | None":
    """apps の値（新スキーマ dict or 旧スキーマ str）から open -a 用 target を取り出す。"""
    if isinstance(entry, dict):
        return entry.get("target")
    if isinstance(entry, str):
        return entry  # 旧スキーマ：値がそのまま target（アプリ名 or exeパス）
    return None


def _nfc(s: str) -> str:
    """NFCに正規化する。

    macOSのファイル名はNFD（濁点が分解：ダ＝タ+゙）で来るため config.auto.json のキーもNFD。
    一方 LLM が出すのはNFC（ダ＝1文字）。突き合わせ前に両側そろえないと、
    『Google カレンダー』のような濁点・半濁点を含む名前の完全一致が外れる。
    """
    return unicodedata.normalize("NFC", s)


def _resolve_entry(apps: dict, name: str):
    """name（表示名 or エイリアス）から apps の項目（dict or str）を解決する。

    解決順：表示名の完全一致 → エイリアス完全一致 → 表示名/エイリアスの大小無視一致。
    比較は常にNFC正規化して行う（macOSのNFDファイル名 vs LLMのNFC を吸収）。
    Windows では起動/終了/配置に exe 名が要るので、target だけでなく項目ごと返す必要がある。
    """
    target = _nfc(name)
    lower = target.lower()
    # 1) 表示名（キー）に完全一致（NFC）
    for key, entry in apps.items():
        if _nfc(key) == target:
            return entry
    # 2) エイリアスに完全一致（NFC）
    for entry in apps.values():
        if isinstance(entry, dict) and any(_nfc(a) == target for a in (entry.get("aliases") or [])):
            return entry
    # 3) 大小無視で表示名/エイリアスに一致（NFC）
    for key, entry in apps.items():
        if _nfc(key).lower() == lower:
            return entry
        if isinstance(entry, dict) and any(_nfc(a).lower() == lower for a in (entry.get("aliases") or [])):
            return entry
    return None


def _resolve_app(apps: dict, name: str) -> "str | None":
    """name から open -a / 起動 用の target 文字列を解決する（_resolve_entry の薄いラッパ）。"""
    return _app_target(_resolve_entry(apps, name))


def _app_exe(apps: dict, name: str) -> str:
    """name から Windows の exe 名（例 Discord.exe）を解決する。無ければ空。"""
    entry = _resolve_entry(apps, name)
    return entry.get("exe", "") if isinstance(entry, dict) else ""


def _app_matches(apps: dict, name: str) -> bool:
    """name が apps（表示名/エイリアス）に該当するか。"""
    return _resolve_app(apps, name) is not None


def open_site(name: str) -> str:
    """名前→URL表からURLを引いてブラウザで開く（OS共通）。

    PWA優先（ADR-0016）：同名のアプリ(PWA)があり、overrides で site 指定されていなければ、
    サイトではなくアプリ起動に委譲する。
    """
    cfg = load_config()
    sites = cfg.get("sites", {})
    apps = cfg.get("apps", {})
    overrides = cfg.get("overrides", {})

    # 衝突時のPWA優先：site強制が無く、アプリ側に該当があればアプリを起動
    if overrides.get(name) != "site" and _app_matches(apps, name):
        return launch_app(name)

    url = sites.get(name)
    if not url:
        return f"『{name}』に対応するサイトが {CONFIG_HINT} にありません。"
    if url.startswith("https://("):  # まだ書き換えてないプレースホルダ
        return f"『{name}』のURLが未設定です（{CONFIG_HINT} を書き換えてください）。"
    _open_in_browser(url)
    return f"{name} を開きました（{url}）。"


def launch_app(name: str) -> str:
    """名前→アプリ表から引いて起動する。Mac は `open -a target`、Windows は exeパス起動。

    apps はエイリアスも含めて解決する。pwa/native とも Mac では `open -a` でOK。
    """
    cfg = load_config()
    apps = cfg.get("apps", {})
    entry = _resolve_entry(apps, name)
    target = _app_target(entry)
    if not target:
        return f"『{name}』に対応するアプリが {CONFIG_HINT} にありません。"
    if "(" in target and not target.startswith("shell:"):  # 旧config等の未設定プレースホルダ（shell:のAppIDは除外）
        return f"『{name}』のパスが未設定です（{CONFIG_HINT} を書き換えてください）。"
    exe = entry.get("exe", "") if isinstance(entry, dict) else ""
    already = _app_is_running(exe if IS_WINDOWS else target)  # 重複起動の判定（Step5(b)）
    try:
        if IS_MAC:
            subprocess.Popen(["open", "-a", target])  # 起動中なら前面化されるだけ（再起動しない）
        elif IS_WINDOWS:
            os.startfile(target)  # .lnk / .exe どちらもシェル経由で起動（既起動なら前面化）
        else:
            subprocess.Popen([target])
        if already:
            return f"{name} は既に開いています（前面に出しました）。"
        return f"{name} を起動しました。"
    except Exception as e:
        return f"{name} の起動に失敗しました：{e}"


# 日本語の言い回し → 正規アクション。OSに依存しない名前で受け、実体はOSネイティブで実行する
# （config.user.json の system 表は Mac の osascript 文字列なので Windows では使えない＝この層で吸収）。
_SYSTEM_ALIASES = {
    "音量下げる": "volume_down", "音量下げて": "volume_down", "ボリューム下げて": "volume_down", "小さく": "volume_down",
    "音量上げる": "volume_up", "音量上げて": "volume_up", "ボリューム上げて": "volume_up", "大きく": "volume_up",
    "ミュート": "mute", "消音": "mute", "ミュート解除": "unmute", "消音解除": "unmute",
    "モニタ消す": "monitor_off", "画面消す": "monitor_off", "モニタオフ": "monitor_off", "画面オフ": "monitor_off",
    "ロック": "lock", "画面ロック": "lock", "ロックして": "lock",
    "スクショ": "screenshot", "スクリーンショット": "screenshot", "画面撮って": "screenshot", "画面を撮って": "screenshot",
    "再生": "media_playpause", "一時停止": "media_playpause", "再生して": "media_playpause", "止めて": "media_playpause",
    "次の曲": "media_next", "スキップ": "media_next", "前の曲": "media_prev", "曲戻して": "media_prev",
    "寝る": "sleep", "スリープ": "sleep", "スリープして": "sleep",
    "再起動": "restart", "再起動して": "restart",
    "シャットダウン": "shutdown", "電源切る": "shutdown", "電源を切る": "shutdown",
}
_DANGEROUS_ACTIONS = {"sleep", "restart", "shutdown"}


def _run_system_native(action: str) -> "str | None":
    """正規アクションをOSネイティブで実行。成功/失敗メッセージを返す。未対応なら None。"""
    if IS_WINDOWS:
        import win_ops
        try:
            return "実行しました。" if win_ops.system_action(action) else None
        except Exception as e:
            return f"失敗しました（{type(e).__name__}）。"
    if IS_MAC:
        mac = {
            "volume_down": "osascript -e 'set volume output volume ((output volume of (get volume settings)) - 12)'",
            "volume_up": "osascript -e 'set volume output volume ((output volume of (get volume settings)) + 12)'",
            "mute": "osascript -e 'set volume output muted true'",
            "unmute": "osascript -e 'set volume output muted false'",
            "monitor_off": "pmset displaysleepnow",
            "sleep": "osascript -e 'tell application \"System Events\" to sleep'",
            "restart": "osascript -e 'tell application \"System Events\" to restart'",
            "shutdown": "osascript -e 'tell application \"System Events\" to shut down'",
        }
        if action in mac:
            subprocess.run(mac[action], shell=True, check=False)
            return "実行しました。"
    return None


def run_system(name: str) -> str:
    """システム操作（音量・モニタ・スリープ等）を実行する。

    日本語の言い回しを正規アクションに寄せ、OSネイティブで実行（nircmd/osascript 非依存）。
    危険操作（スリープ・再起動・シャットダウン）は実行前に確認を挟む（非対話環境では実行しない）。
    既定の語彙に無い名前は、後方互換で config の system / dangerous_system 文字列を試す。
    """
    action = _SYSTEM_ALIASES.get(name.strip()) if name else None

    if action:
        if action in _DANGEROUS_ACTIONS:
            if not sys.stdin or not sys.stdin.isatty():
                return f"『{name}』は危険な操作です。安全のため、この画面からは実行しません（ターミナルから操作してください）。"
            if input(f"⚠ 『{name}』を実行しますか？ [y/N] ").strip().lower() != "y":
                return f"{name} は中止しました。"
        msg = _run_system_native(action)
        if msg is not None:
            return f"{name} を{msg}"

    # 後方互換：既定語彙に無い独自コマンドは config の文字列を実行（Mac等）
    config = load_config()
    safe = config.get("system", {})
    dangerous = config.get("dangerous_system", {})
    if name in safe and not IS_WINDOWS:  # Windowsでは Mac用osascript文字列は使わない
        subprocess.run(safe[name], shell=True, check=False)
        return f"{name} を実行しました。"
    if name in dangerous and not IS_WINDOWS:
        if not sys.stdin or not sys.stdin.isatty():
            return f"『{name}』は危険な操作です。この画面からは実行しません。"
        if input(f"⚠ 『{name}』を実行しますか？ [y/N] ").strip().lower() == "y":
            subprocess.run(dangerous[name], shell=True, check=False)
            return f"{name} を実行しました。"
        return f"{name} は中止しました。"

    return f"『{name}』に対応するシステム操作が見つかりませんでした。"


# このプロセスで ephemeral（一時的）に開いたURLの記録（Step5(b) / ADR-0021）。
# web_search / open_url が開いたURLをここに積み、run_turn が pop して呼び出し側へ渡す。
# 呼び出し側（web.py）は次ターン頭でこれらのタブを自動クローズする。
_EPHEMERAL_OPENED: "list[str]" = []


def pop_ephemeral_opened() -> "list[str]":
    """今ターンで ephemeral に開いたURLを取り出してバッファを空にする。"""
    global _EPHEMERAL_OPENED
    urls = _EPHEMERAL_OPENED
    _EPHEMERAL_OPENED = []
    return urls


def web_search(query: str) -> str:
    """登録ソースに無い情報を『届ける』ための汎用Web検索（Tier2 / ADR-0018）。

    天気・株価・ニュースなど、知りたい情報をブラウザの検索結果として開く。
    捏造防止：答えを想像で言わず、必ず実ソース（検索結果）を開く。Step3で読み上げまで繋げる。
    開いた検索タブは ephemeral 扱い（次ターンで自動クローズ・ADR-0021）。
    """
    if not query or not query.strip():
        return "検索語が空です。"
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
    _open_in_browser(url)
    _EPHEMERAL_OPENED.append(url)
    return f"「{query}」を検索しました。"


def open_url(url: str) -> str:
    """任意のURLをブラウザで開く（Tier2 / ADR-0018）。明確なURLが分かっているときに使う。"""
    if not url or not url.strip():
        return "URLが空です。"
    target = url.strip()
    if not target.startswith(("http://", "https://")):
        target = "https://" + target
    _open_in_browser(target)
    _EPHEMERAL_OPENED.append(target)
    return f"{target} を開きました。"


def _http_get(url: str, timeout: int = 12, ua: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)") -> str:
    """URLを取得して本文文字列を返す。日本語等の非ASCIIはエンコードしてから投げる。
    ua=curl 系にすると wttr.in 等は HTML でなくプレーンテキストを返す。"""
    safe = urllib.parse.quote(url, safe="%/:=&?#+@!$,;'()*~[]")
    req = urllib.request.Request(safe, headers={"User-Agent": ua})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")


def fetch_page(url: str) -> str:
    """URLの本文を取得しプレーンテキストで返す（B：内容を読んで“伝える”ための材料・ADR-0021）。

    返したテキストをモデルが要約して伝える。捏造防止：実際に取得した内容だけを根拠にする。
    検索結果ページ（google等）はボット遮断で取れないことが多い。具体的な記事/サイトのURL向き。
    """
    if not url or not url.strip():
        return "URLが空です。"
    target = url.strip()
    if not target.startswith(("http://", "https://")):
        target = "https://" + target
    try:
        raw = _http_get(target)
    except Exception as e:
        return f"取得に失敗しました（{type(e).__name__}）。"
    text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text[:3000] if text else "（本文が取得できませんでした）"


def _weather_desc(obj: dict) -> str:
    """wttr.in j1 の天気説明。lang_ja があれば日本語、無ければ英語にフォールバック。"""
    for key in ("lang_ja", "weatherDesc"):
        arr = obj.get(key)
        if arr and isinstance(arr, list) and arr[0].get("value"):
            return arr[0]["value"].strip()
    return "?"


def _weather_summary(loc: str, data: dict) -> str:
    """j1 JSON → 現在＋3日分（3時間毎）のコンパクトな実データ要約（モデルが読む材料）。"""
    cur = (data.get("current_condition") or [{}])[0]
    lines = [
        f"{loc} の天気（実データ・wttr.in）:",
        f"現在: {_weather_desc(cur)} {cur.get('temp_C', '?')}°C 体感{cur.get('FeelsLikeC', '?')}°C "
        f"湿度{cur.get('humidity', '?')}% 風{cur.get('windspeedKmph', '?')}km/h",
    ]
    labels = ["今日", "明日", "明後日"]
    for i, day in enumerate((data.get("weather") or [])[:3]):
        parts = []
        for h in day.get("hourly", []):
            try:
                t = int(h.get("time", "0")) // 100
            except ValueError:
                continue
            parts.append(f"{t}時:{_weather_desc(h)}{h.get('tempC', '?')}°/降水{h.get('chanceofrain', '?')}%")
        lines.append(
            f"{labels[i] if i < 3 else ''}({day.get('date', '?')}) "
            f"最低{day.get('mintempC', '?')}°〜最高{day.get('maxtempC', '?')}°: " + " ".join(parts)
        )
    return "\n".join(lines)


# 天気ページを「見せる」か。音声モード(voice.py)は読み上げが配信手段なのでタブを開かない(False)。
SHOW_WEATHER_PAGE = True


def activate_app(name: str) -> None:
    """アプリを前面化（一時タブ後片付けの後に元の画面へ戻す用）。失敗しても無害。"""
    if not IS_MAC or not name:
        return
    _osa(f'tell application "{name}" to activate')


def get_weather(location: str = "") -> str:
    """指定地の天気（現在＋3日先までの3時間毎予報）を実データで取得して伝える（ADR-0021/0029）。

    - 「明日の天気」「明日雨大丈夫？」に答えられるよう、j1(JSON) で予報まで取る。
    - location が空ならプロファイルの既定地名（例: 茨木）を使う＝場所未指定でも答えられる。
    - 返り値の実データをモデルが日本語で要約して伝える（捏造防止）。
    “見せる”用の天気ページは SHOW_WEATHER_PAGE が真の時だけ開く（音声モードは開かない）。
    """
    loc = (location or "").strip() or profile_store.default_location() or "現在地"
    enc = urllib.parse.quote(loc)
    page = f"https://wttr.in/{enc}"
    try:
        raw = _http_get(f"https://wttr.in/{enc}?format=j1&lang=ja", ua="curl/8.0")
        summary = _weather_summary(loc, json.loads(raw))
    except Exception:
        # 予報(JSON)が取れない時は従来の1行（現在のみ）にフォールバック
        try:
            data = _http_get(f"https://wttr.in/{enc}?format=%l:+%c+%t+%w+%h+%p&m", ua="curl/8.0").strip()
            summary = f"天気（実データ・現在のみ）: {data}"
        except Exception as e:
            return f"{loc} の天気が取得できませんでした（{type(e).__name__}）。"
    if SHOW_WEATHER_PAGE:
        _open_in_browser(page)           # 見せる
        _EPHEMERAL_OPENED.append(page)   # 用が済んだら次ターンで閉じる
    return summary


def play_media(query: str, kind: str = "video") -> str:
    """動画/音楽を検索ディープリンクで開いて『残す』（C・persistent / #22）。

    「この動画」「あれ流して」→ 適切なサービスの検索URLを開く。
    persistent（ADR-0021）なので _EPHEMERAL_OPENED に積まない＝自動クローズしない＝残す。
    - video: YouTube 検索結果
    - music: ネイティブ Spotify（spotify:search:）優先、無ければ Spotify Web 検索
    """
    q = (query or "").strip()
    if not q:
        return "何を再生するか分かりませんでした（曲名・動画名を教えてください）。"
    k = (kind or "video").strip().lower()
    if k in ("music", "song", "audio", "音楽", "曲"):
        apps = load_config().get("apps", {})
        if IS_MAC and _resolve_app(apps, "Spotify"):  # ネイティブSpotifyがあればアプリ内検索
            subprocess.Popen(["open", f"spotify:search:{urllib.parse.quote(q)}"])
            return f"Spotify で「{q}」を検索しました（流せます・残します）。"
        url = "https://open.spotify.com/search/" + urllib.parse.quote(q)
        _open_in_browser(url)
        return f"Spotify（Web）で「{q}」を開きました（残します）。"
    # 既定：動画 → YouTube 検索
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(q)
    _open_in_browser(url)
    return f"YouTube で「{q}」を検索して開きました（残します）。"


# --------------------------------------------------------------------------
# 永続記憶（remember / add_schedule / forget）— 実体は profile_store（ADR-0029）
# --------------------------------------------------------------------------
def remember(fact: str) -> str:
    """個人情報・事実を profile.json に永続記憶する（「〜を覚えておいて」）。"""
    return profile_store.remember(fact)


def add_schedule(title: str, weekday: str = "", date: str = "", time: str = "", once: bool = False) -> str:
    """予定を登録する。weekday=毎週の繰り返し / date=日付指定（YYYY-MM-DD）/ weekday+once=その曜日に1回だけ。"""
    return profile_store.add_schedule(title, weekday=weekday, date=date, time=time, once=once)


def forget(query: str) -> str:
    """query に該当する記憶・予定を削除する。"""
    return profile_store.forget(query)


def _osa(script: str) -> "tuple[bool, str]":
    """AppleScript を実行。(成功?, 出力or標準エラー) を返す。"""
    p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if p.returncode == 0:
        return True, p.stdout.strip()
    return False, (p.stderr or p.stdout).strip()


def _ax_denied(err: str) -> bool:
    """アクセシビリティ未許可のエラーか判定（-1719 / assistive access）。"""
    e = err.lower()
    return "-1719" in e or "assistive" in e or "not allowed" in e or "1002" in e


def _front_process() -> str:
    ok, name = _osa('tell application "System Events" to get name of first process whose frontmost is true')
    return name if ok else ""


def _ui_processes() -> "list[str]":
    """UIを持つ（background only でない）プロセス名の一覧。"""
    ok, out = _osa('tell application "System Events" to get name of every process whose background only is false')
    return [p.strip() for p in out.split(",")] if (ok and out) else []


# 起動中アプリ一覧から除くノイズ（システムUI・開発ツール・自分自身）
_RUNNING_IGNORE = {
    "Finder", "Terminal", "iTerm2", "Claude", "Code", "parsecd", "Copilot",
    "Dock", "Control Center", "Control Centre", "Spotlight", "SystemUIServer",
    "Notification Center", "Notification Centre", "WindowManager", "loginwindow",
    "app_mode_loader",
}


def running_apps() -> "list[str]":
    """いま起動中でUIを持つネイティブアプリ名の一覧（Activity Monitor の軽量版 / Issue #20）。

    System Events のプロセス名一覧（1回・高速）からノイズ（システムUI・開発ツール・自分自身）を除く。
    注意: Chrome PWA は実体が "app_mode_loader" に潰れて個別判別できないため、ここには出ない
    （PWAの開閉確実性は launch_app の idempotent 起動＝開いていれば前面化、で担保する）。
    エージェントが「いま何が開いているか」を把握し、再起動や“やってない配置”の捏造を避けるための材料。
    """
    if IS_WINDOWS:
        import win_ops
        return win_ops.running_apps(load_config())
    if not IS_MAC:
        return []
    seen, res = set(), []
    for it in _ui_processes():
        it = it.strip()
        if not it or it in _RUNNING_IGNORE or it in seen:
            continue
        seen.add(it)
        res.append(it)
    return res


def _window_process(app: str) -> str:
    """app名（表示名/エイリアス）→ 実在する System Events プロセス名に解決。空なら最前面。

    config 解決名（例: Chrome→"Google Chrome"）と実プロセス名のズレを、実在プロセス一覧と
    突き合わせて吸収する（完全一致→NFC→大小無視→部分一致）。
    """
    if not app or not app.strip():
        return _front_process()
    target = _resolve_app(load_config().get("apps", {}), app) or app
    name = Path(target).name
    if name.endswith(".app"):
        name = name[:-4]
    procs = _ui_processes()
    if not procs:
        return name
    cands = {name, app.strip()}  # 解決名と素の入力の両方で当てにいく
    for c in cands:
        cn = _nfc(c)
        for p in procs:  # 完全一致（NFC）
            if _nfc(p) == cn:
                return p
        for p in procs:  # 大小無視
            if _nfc(p).lower() == cn.lower():
                return p
        for p in procs:  # 部分一致（Chrome ⊂ Google Chrome）
            pl, cl = _nfc(p).lower(), cn.lower()
            if cl in pl or pl in cl:
                return p
    return name


def _visible_frame() -> "tuple[int, int, int, int]":
    """メインディスプレイの可視領域 (x, y, w, h) を近似で返す（メニューバー分を上から除く）。"""
    ok, out = _osa('tell application "Finder" to get bounds of window of desktop')
    try:
        x0, y0, x1, y1 = [int(v) for v in out.replace(" ", "").split(",")]
    except Exception:
        x0, y0, x1, y1 = 0, 0, 1440, 900
    menubar = 25  # メニューバー高さの近似
    return x0, y0 + menubar, x1 - x0, y1 - y0 - menubar


def _list_windows() -> str:
    fx, fy, fw, fh = _visible_frame()
    procs = _ui_processes()
    apps = "、".join(procs) if procs else "(取得失敗)"
    return f"メイン画面: {fw}x{fh}（左上 {fx},{fy}）。表示中のアプリ: {apps}"


# 純正タイル（macOS 15+ の Window メニュー）。"Move & Resize" 配下 or トップレベル(Fill)。
# 自前の座標指定はアプリ最小サイズと喧嘩して隙間が出るが、純正タイルは隙間なしの左右ピッタリ。
_NATIVE_TILE = {
    "left": ("Left", "Move & Resize"),
    "right": ("Right", "Move & Resize"),
    "maximize": ("Fill", None),  # Fill はトップレベル項目
}


def _native_tile(proc: str, item: str, submenu: "str | None") -> "tuple[bool, str]":
    """Window メニューの純正タイルを叩く（前面化→本体ウィンドウを最前面に→メニュー項目クリック）。

    Discord等は小さな副ウィンドウを複数持ち、それにタイルが当たると本体が並ばず重なる。
    クリック前に「一番大きいウィンドウ＝本体」をAXRaiseして、本体に確実に当てる。
    """
    if submenu:
        click = (
            f'click menu item "{item}" of menu 1 of menu item "{submenu}" '
            f'of menu 1 of menu bar item "Window" of menu bar 1'
        )
    else:
        click = f'click menu item "{item}" of menu 1 of menu bar item "Window" of menu bar 1'
    script = (
        f'tell application "System Events" to tell process "{proc}"\n'
        f'  set frontmost to true\n'
        f'  delay 0.35\n'
        f'  try\n'
        f'    set best to missing value\n'
        f'    set bestArea to -1\n'
        f'    repeat with w in windows\n'
        f'      set sz to size of w\n'
        f'      set a to (item 1 of sz) * (item 2 of sz)\n'
        f'      if a > bestArea then\n'
        f'        set bestArea to a\n'
        f'        set best to w\n'
        f'      end if\n'
        f'    end repeat\n'
        f'    if best is not missing value then\n'
        f'      perform action "AXRaise" of best\n'
        f'      delay 0.15\n'
        f'    end if\n'
        f'  end try\n'
        f'  {click}\n'
        f'end tell'
    )
    return _osa(script)


_WINDOW_ACTIONS = {
    "left": "左半分", "右": "right", "左": "left", "right": "right",
    "maximize": "最大化", "最大": "maximize", "最大化": "maximize", "全画面": "maximize",
    "center": "中央", "中央": "center", "真ん中": "center", "list": "一覧", "一覧": "list",
}


def _manage_window_win(action: str, app: str = "", monitor: str = "") -> str:
    """Windows のウィンドウ配置（ctypes 経由・win_ops に委譲）。Mac の純正タイル相当。

    monitor: "left"/"right"/番号 で配置先モニタを指定（「左の画面にDiscord」）。空なら現在の画面。
    対象アプリが起動していなければ一度起動し、窓ができるのを待ってから配置する（Mac版と同じ自己修復）。
    """
    import win_ops
    raw = (action or "").strip()
    act = _WINDOW_ACTIONS.get(raw, raw.lower())
    act = act if act in ("left", "right", "maximize", "center", "list") else raw.lower()
    if act == "list":
        return "、".join(running_apps()) or "(起動中のアプリは把握できませんでした)"
    if act not in ("left", "right", "maximize", "center"):
        return "未対応の操作です（left / right / maximize / center / list）。"

    exe = ""
    if app and app.strip():
        exe = _app_exe(load_config().get("apps", {}), app)
        # 対象が起動していなければ起動して窓の生成を待つ（PWA未起動でも自己修復）
        if exe and not win_ops.app_is_running(exe):
            launch_app(app)
            time.sleep(1.3)

    label = {"left": "左半分", "right": "右半分", "maximize": "最大化", "center": "中央"}[act]
    where = {"left": "左の画面の", "right": "右の画面の"}.get((monitor or "").strip().lower(), "")
    ok, msg = win_ops.manage_window(act, exe, monitor)
    if ok:
        who = app.strip() if (app and app.strip()) else "最前面のウィンドウ"
        return f"{who} を{where}{label}に配置しました。"
    return f"ウィンドウを{where}{label}に配置できませんでした（{msg}）。"


def manage_window(action: str, app: str = "", monitor: str = "", _relaunched: bool = False) -> str:
    """ウィンドウを配置する（A+D・要アクセシビリティ許可 / #23・ADR-0025）。

    monitor: "left"/"right"/番号 で配置先モニタを指定（Windows）。Mac は単一画面前提で無視。

    action: left（左半分）/ right（右半分）/ maximize（最大化）/ center（中央）/ list（一覧）。
    app: 対象アプリ名（省略時は最前面のウィンドウ）。対象が開いていなければ自動で起動して並べる。
    メインディスプレイ前提（マルチモニタ配置は別トラック=Hammerspoon）。
    """
    if IS_WINDOWS:
        return _manage_window_win(action, app, monitor)
    if not IS_MAC:
        return "ウィンドウ管理はいまの環境では未対応です（Mac / Windows のみ）。"
    raw = (action or "").strip()
    act = _WINDOW_ACTIONS.get(raw, raw.lower())
    act = act if act in ("left", "right", "maximize", "center", "list") else raw.lower()
    if act == "list":
        return _list_windows()
    if act not in ("left", "right", "maximize", "center"):
        return "未対応の操作です（left / right / maximize / center / list）。"

    proc = _window_process(app)
    if not proc:
        return "対象のウィンドウが分かりませんでした。"
    label = {"left": "左半分", "right": "右半分", "maximize": "最大化", "center": "中央"}[act]

    # ① まず純正タイル（隙間なしの左右ピッタリ／画面いっぱい）。起動直後の競合に備えてリトライ。
    if act in _NATIVE_TILE:
        item, submenu = _NATIVE_TILE[act]
        ok, err = False, ""
        for _ in range(4):
            ok, err = _native_tile(proc, item, submenu)
            if ok or _ax_denied(err):
                break
            time.sleep(0.5)
        if ok:
            return f"{proc} を{label}に配置しました。"
        if _ax_denied(err):
            return (
                "ウィンドウを動かす権限（アクセシビリティ）が未許可です。"
                "システム設定 → プライバシーとセキュリティ → アクセシビリティ で、"
                "この端末（Terminal/iTerm 等）を許可してください。"
            )
        # 純正タイルが無い/効かない時は ② 座標方式へフォールバック

    # ② フォールバック（古いOS・純正メニュー非対応アプリ・center）：座標で配置
    fx, fy, fw, fh = _visible_frame()
    if act == "left":
        x, y, w, h = fx, fy, fw // 2, fh
    elif act == "right":
        x, y, w, h = fx + fw // 2, fy, fw - fw // 2, fh
    elif act == "maximize":
        x, y, w, h = fx, fy, fw, fh
    else:  # center
        w, h = int(fw * 0.7), int(fh * 0.8)
        x, y = fx + (fw - w) // 2, fy + (fh - h) // 2

    script = (
        f'tell application "System Events" to tell process "{proc}"\n'
        f'  set position of window 1 to {{{x}, {y}}}\n'
        f'  set size of window 1 to {{{w}, {h}}}\n'
        f'end tell'
    )
    ok, err = False, ""
    for _ in range(4):
        ok, err = _osa(script)
        if ok or _ax_denied(err):
            break
        time.sleep(0.5)
    if ok:
        return f"{proc} のウィンドウを{label}に配置しました。"
    if _ax_denied(err):
        return (
            "ウィンドウを動かす権限（アクセシビリティ）が未許可です。"
            "システム設定 → プライバシーとセキュリティ → アクセシビリティ で、"
            "この端末（Terminal/iTerm 等）を許可してください。"
        )
    # 対象アプリが開いていない可能性 → 起動して一度だけ並べ直す（PWA未起動でも自己修復）
    if app and app.strip() and not _relaunched:
        target = _resolve_app(load_config().get("apps", {}), app)
        if target:
            try:
                subprocess.Popen(["open", "-a", target])
            except Exception:
                pass
            time.sleep(1.3)  # ウィンドウ生成待ち
            return manage_window(action, app, _relaunched=True)
    return f"{proc} のウィンドウ操作に失敗しました（ウィンドウが無い可能性）。"


def _close_app_win(name: str = "") -> str:
    """Windows のアプリ/サイトを閉じる。ウィンドウへ WM_CLOSE（native/UWP/PWA 何でも・安全）を
    第一手段にし、窓が見つからない native アプリは taskkill でフォールバック。"""
    import win_ops
    target_name = (name or "").strip()
    if not target_name:
        return "何を閉じますか？（アプリ名やサイト名を教えてください）"
    cfg = load_config()
    entry = _resolve_entry(cfg.get("apps", {}), target_name)

    # ① タイトル一致のウィンドウを閉じる（種類問わず）。候補＝入力語・表示名・英名・エイリアス・サイト名。
    cands = [target_name]
    if isinstance(entry, dict):
        cands.append(entry.get("name", ""))
        cands.append(Path(entry.get("exe", "")).stem)
        cands += entry.get("aliases", []) or []
    sites = cfg.get("sites", {})
    if target_name in sites:  # サイト名（PWA/タブのタイトルに出ることが多い）
        cands.append(target_name)

    # ⓪ Chrome のタブとして開いているなら、まずタブ単位で閉じる（CDP・ブラウザ本体は巻き込まない）
    try:
        import cdp
        closed = cdp.close_by_title([c for c in cands if c])
        if closed:
            return f"{target_name} のタブを閉じました。"
    except Exception:
        pass

    if win_ops.close_window_by_title(cands):
        return f"{target_name} を閉じました。"

    # ② 窓が見つからない場合：native exe なら taskkill
    exe = entry.get("exe", "") if isinstance(entry, dict) else ""
    if exe and win_ops.close_exe(exe):
        return f"{target_name} を閉じました。"
    if entry is not None or target_name in sites:
        return f"{target_name} は開いていないようです。"
    return f"『{target_name}』に対応するアプリ/サイトが見つかりませんでした。"


def close_app(name: str = "") -> str:
    """開いているアプリ／サイトを閉じる（「YouTube閉じて」「Discordやめて」等）。

    - サイトとして開いているタブ → 該当URLのタブを閉じる
    - アプリ → quit（PWA/native とも）
    『閉じる/消す/やめる』の動詞をここで正しく受ける（モニタ消す等の誤爆防止）。
    """
    if IS_WINDOWS:
        return _close_app_win(name)
    if not IS_MAC:
        return "閉じる操作はいまの環境では未対応です（Mac / Windows のみ）。"
    target_name = (name or "").strip()
    if not target_name:
        return "何を閉じますか？（アプリ名やサイト名を教えてください）"
    cfg = load_config()
    # 1) サイトとして開いているタブを閉じる
    sites = cfg.get("sites", {})
    url = sites.get(target_name) or sites.get(target_name.lower())
    if url and close_browser_tabs([url]):
        return f"{target_name} のタブを閉じました。"
    # 2) アプリとして quit
    resolved = _resolve_app(cfg.get("apps", {}), target_name)
    if resolved:
        appname = Path(resolved).name
        if appname.endswith(".app"):
            appname = appname[:-4]
        ok, _ = _osa(f'tell application "{appname}" to quit')
        if ok:
            return f"{appname} を閉じました。"
        return f"{target_name} を閉じられませんでした。"
    if url:
        return f"{target_name} は開いていないようです。"
    return f"『{target_name}』に対応するアプリ/サイトが見つかりませんでした。"


def close_browser_tabs(urls: "list[str]") -> int:
    """指定URLに一致するブラウザタブを閉じる（ephemeralの後片付け）。

    Windows: CDP（タブ管理できる Chrome）経由。CDP 無しなら 0（閉じられない＝正直に劣化）。
    Mac: AppleScript。初回は「"Terminal"が"Google Chrome"を制御」許可が要る。
    閉じた数を返す。失敗しても本体を止めない。
    """
    if IS_WINDOWS and urls:
        try:
            import cdp
            return cdp.close_by_url(urls)
        except Exception:
            return 0
    if not IS_MAC or not urls:
        return 0
    closed = 0
    for url in urls:
        safe = url.replace("\\", "\\\\").replace('"', '\\"')
        script = f'''
        tell application "Google Chrome"
          set c to 0
          if it is running then
            repeat with w in windows
              set tl to tabs of w
              repeat with k from (count of tl) to 1 by -1
                if (URL of (item k of tl)) contains "{safe}" then
                  close (item k of tl)
                  set c to c + 1
                end if
              end repeat
            end repeat
          end if
          return c
        end tell'''
        try:
            r = subprocess.run(["osascript", "-e", script], check=False,
                               capture_output=True, text=True, timeout=8)
            closed += int(r.stdout.strip() or 0)
        except Exception:
            pass
    return closed


def _app_is_running(target: str) -> bool:
    """アプリが既に起動中か（重複起動の判定）。Mac=pgrep / Windows=tasklist。判定不能なら False。

    Mac は target にアプリ名、Windows は exe 名（例 Discord.exe）を渡す前提。
    """
    if IS_WINDOWS:
        import win_ops
        return win_ops.app_is_running(target)
    if not IS_MAC:
        return False
    try:
        r = subprocess.run(["pgrep", "-x", target], check=False, capture_output=True, text=True, timeout=4)
        return r.returncode == 0
    except Exception:
        return False


def monitor_count() -> int:
    """接続モニタ数（プロンプトで『画面は◯枚』と伝えるため）。Windows以外は1とみなす。"""
    if not IS_WINDOWS:
        return 1
    try:
        import win_ops
        return win_ops.monitor_count()
    except Exception:
        return 1


def windows_summary() -> str:
    """『どの画面に何のウィンドウがあるか』の1行要約（毎ターンLLMへ渡す材料・⑦）。

    例: "mon1: 電卓 / GitHub - Chrome｜mon2: Discord"。Windows以外・失敗時は空文字。
    """
    if not IS_WINDOWS:
        return ""
    try:
        import win_ops
        wins = win_ops.windows_overview()
    except Exception:
        return ""
    if not wins:
        return ""
    by_mon: dict = {}
    for idx, title in wins:
        by_mon.setdefault(idx, []).append(title)
    return "｜".join(f"mon{i}: " + " / ".join(ts) for i, ts in sorted(by_mon.items()))


# Claude に渡すツール定義（tool use）
TOOL_DEFS = [
    {
        "name": "open_site",
        "description": "Open a website. name = key in sites config (e.g. claude, 課題, 動画).",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "launch_app",
        "description": "Launch an installed app. name = app display name or alias.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "run_system",
        "description": "Run a system action (volume/display/sleep). name = key in system config.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "web_search",
        "description": "Open web search results for info not in registered sites/apps (weather/stocks/news/lookups). Never invent answers. query = search terms.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "open_url",
        "description": "Open a specific URL. url = the URL.",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "get_weather",
        "description": "Get real weather (current + forecast) for any weather/temp/rain question. location = place name; map words like 大学/学校 via the profile place table; empty = default place.",
        "input_schema": {"type": "object", "properties": {"location": {"type": "string"}}},
    },
    {
        "name": "fetch_page",
        "description": "Fetch a URL's body text to read/summarize a specific article. url = the URL.",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "play_media",
        "description": "Search & open video/music (stays open). query = title/artist. kind = video (YouTube) or music (Spotify); default video.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "kind": {"type": "string", "enum": ["video", "music"]}},
            "required": ["query"],
        },
    },
    {
        "name": "manage_window",
        "description": "Tile/place a window. app empty = frontmost. monitor: which screen (left/right/number, "
                       "empty = current). Usage rules are in the system prompt.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["left", "right", "maximize", "center", "list"]},
                "app": {"type": "string"},
                "monitor": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "remember",
        "description": "Persistently remember a fact about the user. fact = a self-contained sentence. Use add_schedule (not this) for anything with a weekday/date.",
        "input_schema": {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]},
    },
    {
        "name": "add_schedule",
        "description": "Save an event. Weekly: weekday(月〜日). One-off next given weekday (来週の○曜): "
                       "weekday+once=true (date stays empty; system computes it). Specific day: date=YYYY-MM-DD.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "weekday": {"type": "string"},
                "once": {"type": "boolean"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "time": {"type": "string", "description": "HH:MM"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "forget",
        "description": "Delete a remembered fact or schedule. query = substring identifying the target.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "close_app",
        "description": "Close an open app/site (閉じて/消して/やめて). name = target app/site; use the just-opened one from context.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
]

# ツール名 → 実関数
DISPATCH = {
    "open_site": open_site,
    "launch_app": launch_app,
    "run_system": run_system,
    "web_search": web_search,
    "open_url": open_url,
    "get_weather": get_weather,
    "fetch_page": fetch_page,
    "play_media": play_media,
    "manage_window": manage_window,
    "close_app": close_app,
    "remember": remember,
    "add_schedule": add_schedule,
    "forget": forget,
}


def run_tool(name: str, tool_input: dict) -> str:
    func = DISPATCH.get(name)
    if not func:
        return f"未知のツール: {name}"
    return func(**tool_input)
