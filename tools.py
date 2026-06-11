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
import platform
import re
import subprocess
import unicodedata
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

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


def _resolve_app(apps: dict, name: str) -> "str | None":
    """name（表示名 or エイリアス）から target を解決する。

    解決順：表示名の完全一致 → エイリアス完全一致 → 表示名/エイリアスの大小無視一致。
    比較は常にNFC正規化して行う（macOSのNFDファイル名 vs LLMのNFC を吸収）。
    site のキーは小文字（youtube/github 等）、アプリ表示名は大小混在（YouTube/GitHub）
    なので、衝突検出やLLMの表記ゆれを拾えるよう最後に大小無視でも寄せる。
    """
    target = _nfc(name)
    lower = target.lower()
    # 1) 表示名（キー）に完全一致（NFC）
    for key, entry in apps.items():
        if _nfc(key) == target:
            return _app_target(entry)
    # 2) エイリアスに完全一致（NFC）
    for entry in apps.values():
        if isinstance(entry, dict) and any(_nfc(a) == target for a in (entry.get("aliases") or [])):
            return _app_target(entry)
    # 3) 大小無視で表示名/エイリアスに一致（NFC）
    for key, entry in apps.items():
        if _nfc(key).lower() == lower:
            return _app_target(entry)
        if isinstance(entry, dict) and any(_nfc(a).lower() == lower for a in (entry.get("aliases") or [])):
            return _app_target(entry)
    return None


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
    webbrowser.open(url)
    return f"{name} を開きました（{url}）。"


def launch_app(name: str) -> str:
    """名前→アプリ表から引いて起動する。Mac は `open -a target`、Windows は exeパス起動。

    apps はエイリアスも含めて解決する。pwa/native とも Mac では `open -a` でOK。
    """
    cfg = load_config()
    apps = cfg.get("apps", {})
    target = _resolve_app(apps, name)
    if not target:
        return f"『{name}』に対応するアプリが {CONFIG_HINT} にありません。"
    if "(" in target:  # 旧Windows config 等の未設定プレースホルダ
        return f"『{name}』のパスが未設定です（{CONFIG_HINT} を書き換えてください）。"
    already = _app_is_running(target)  # 重複起動の判定（Step5(b)）
    try:
        if IS_MAC:
            subprocess.Popen(["open", "-a", target])  # 起動中なら前面化されるだけ（再起動しない）
        else:
            subprocess.Popen([target])
        if already:
            return f"{name} は既に開いています（前面に出しました）。"
        return f"{name} を起動しました。"
    except Exception as e:
        return f"{name} の起動に失敗しました：{e}"


def run_system(name: str) -> str:
    """名前→システムコマンド表を実行。危険コマンドは別表に隔離し、実行前に確認を挟む。"""
    config = load_config()
    safe = config.get("system", {})
    dangerous = config.get("dangerous_system", {})

    if name in safe:
        try:
            subprocess.run(safe[name], shell=True, check=False)
            return f"{name} を実行しました。"
        except Exception as e:
            return f"{name} の実行に失敗しました：{e}"

    if name in dangerous:
        # 危険操作は即実行せず確認を挟む（自律性の制限：暴走防止）
        ans = input(f"⚠ 『{name}』は危険な操作です。実行しますか？ [y/N] ").strip().lower()
        if ans == "y":
            subprocess.run(dangerous[name], shell=True, check=False)
            return f"{name} を実行しました。"
        return f"{name} は中止しました。"

    return f"『{name}』に対応するシステムコマンドが {CONFIG_HINT} にありません。"


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
    webbrowser.open(url)
    _EPHEMERAL_OPENED.append(url)
    return f"「{query}」を検索しました。"


def open_url(url: str) -> str:
    """任意のURLをブラウザで開く（Tier2 / ADR-0018）。明確なURLが分かっているときに使う。"""
    if not url or not url.strip():
        return "URLが空です。"
    target = url.strip()
    if not target.startswith(("http://", "https://")):
        target = "https://" + target
    webbrowser.open(target)
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


def get_weather(location: str) -> str:
    """指定地のいまの天気を取得して伝える（B/ADR-0021・ソース=wttr.in、キー不要・実データ）。

    返り値の実データをモデルが日本語で要約して伝える（捏造防止）。
    “見せる”用に天気ページもブラウザで開く（ephemeral：次ターンで自動クローズ）。
    """
    loc = (location or "").strip() or "現在地"
    enc = urllib.parse.quote(loc)
    try:
        data = _http_get(f"https://wttr.in/{enc}?format=%l:+%c+%t+%w+%h+%p&m", ua="curl/8.0").strip()
    except Exception as e:
        return f"{loc} の天気が取得できませんでした（{type(e).__name__}）。"
    page = f"https://wttr.in/{enc}"
    webbrowser.open(page)            # 見せる
    _EPHEMERAL_OPENED.append(page)   # 用が済んだら次ターンで閉じる
    return f"天気（実データ）: {data}"


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
        webbrowser.open(url)
        return f"Spotify（Web）で「{q}」を開きました（残します）。"
    # 既定：動画 → YouTube 検索
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(q)
    webbrowser.open(url)
    return f"YouTube で「{q}」を検索して開きました（残します）。"


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


_WINDOW_ACTIONS = {
    "left": "左半分", "右": "right", "左": "left", "right": "right",
    "maximize": "最大化", "最大": "maximize", "最大化": "maximize", "全画面": "maximize",
    "center": "中央", "中央": "center", "真ん中": "center", "list": "一覧", "一覧": "list",
}


def manage_window(action: str, app: str = "") -> str:
    """ウィンドウを配置する（A+D・要アクセシビリティ許可 / #23・ADR-0025）。

    action: left（左半分）/ right（右半分）/ maximize（最大化）/ center（中央）/ list（一覧）。
    app: 対象アプリ名（省略時は最前面のウィンドウ）。
    メインディスプレイ前提（マルチモニタ配置は別トラック=Hammerspoon）。
    """
    if not IS_MAC:
        return "ウィンドウ管理はいまの環境では未対応です（Mac のみ）。"
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
    ok, err = _osa(script)
    if ok:
        label = {"left": "左半分", "right": "右半分", "maximize": "最大化", "center": "中央"}[act]
        return f"{proc} のウィンドウを{label}に配置しました。"
    if _ax_denied(err):
        return (
            "ウィンドウを動かす権限（アクセシビリティ）が未許可です。"
            "システム設定 → プライバシーとセキュリティ → アクセシビリティ で、"
            "この端末（Terminal/iTerm 等）を許可してください。"
        )
    return f"{proc} のウィンドウ操作に失敗しました（ウィンドウが無い可能性）。"


def close_browser_tabs(urls: "list[str]") -> int:
    """指定URLに一致するブラウザタブを閉じる（ephemeralの後片付け・Mac/Chrome）。

    閉じた数を返す。Chrome以外・未起動・権限未許可などは静かに0で返す（本体を止めない）。
    ※初回は macOS の「"Terminal"が"Google Chrome"を制御」許可が要る。
    """
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
    """アプリが既に起動中か（重複起動の判定・Mac）。権限不要の pgrep で判定。判定不能なら False。"""
    if not IS_MAC:
        return False
    try:
        r = subprocess.run(["pgrep", "-x", target], check=False, capture_output=True, text=True, timeout=4)
        return r.returncode == 0
    except Exception:
        return False


# Claude に渡すツール定義（tool use）
TOOL_DEFS = [
    {
        "name": "open_site",
        "description": "ブラウザでサイトを開く。引数 name は設定の sites 表のキー（例: claude, 課題, 動画）。表記ゆれは近いキーに寄せて解釈する。",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "開くサイトの名前"}},
            "required": ["name"],
        },
    },
    {
        "name": "launch_app",
        "description": "PCのアプリを起動する。引数 name は設定の apps 表のキー（表示名）かその日本語エイリアス（例: ディスコ, ようつべ, メール）。表記ゆれは近いキー/エイリアスに寄せて解釈する。",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "起動するアプリの名前（表示名 or エイリアス）"}},
            "required": ["name"],
        },
    },
    {
        "name": "run_system",
        "description": "PCのシステム操作（音量・モニタ・スリープ等）を実行する。引数 name は設定の system / dangerous_system 表のキー（例: 音量下げる, モニタ消す, 寝る）。",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "実行するシステム操作の名前"}},
            "required": ["name"],
        },
    },
    {
        "name": "web_search",
        "description": "知りたい情報をブラウザの検索で開く。登録済みのサイト/アプリで直接得られない情報（天気・株価・ニュース・調べもの等）を扱うとき、または『〜どう？/〜は？/調べて/教えて』のように答えを知りたいときに使う。答えを想像で言わず必ずこれで実ソースを開く（捏造防止）。引数 query は検索語（例: 茨木 天気, NVIDIA 株価）。",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "検索語"}},
            "required": ["query"],
        },
    },
    {
        "name": "open_url",
        "description": "指定されたURLをブラウザで開く。明確なURLが分かっているときに使う。引数 url は開くURL。",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "開くURL"}},
            "required": ["url"],
        },
    },
    {
        "name": "get_weather",
        "description": "指定した地名の『いまの天気』を実データで取得して伝える。天気を聞かれたら必ずこれを使う（想像で答えない）。引数 location は地名（例: 茨木, Tokyo）。取得した実データを日本語で要約して伝えること。",
        "input_schema": {
            "type": "object",
            "properties": {"location": {"type": "string", "description": "地名"}},
            "required": ["location"],
        },
    },
    {
        "name": "fetch_page",
        "description": "指定URLの本文を取得して返す。具体的な記事/サイトの内容を読んで要約・回答したいときに使う（戻り値の実データだけを根拠に要約する＝捏造防止）。検索結果ページ(google等)はボット遮断で取れないことが多い。引数 url は取得するURL。",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "取得するURL"}},
            "required": ["url"],
        },
    },
    {
        "name": "play_media",
        "description": "動画や音楽を検索して開く（開いたら残す＝自動で閉じない）。「〜の動画見たい」「〜流して/聴きたい」「あれ流して」などメディアを見聞きしたい時に使う。query は曲名・動画名・アーティスト等。kind は 'video'（YouTube）か 'music'（Spotify）。迷ったら video。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "曲名・動画名・アーティスト名など"},
                "kind": {"type": "string", "enum": ["video", "music"], "description": "video=動画 / music=音楽"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "manage_window",
        "description": "ウィンドウを画面に配置する（Macのみ・要アクセシビリティ許可）。「左/右に寄せて」「最大化」「中央に」「ウィンドウ一覧」など。action は left/right/maximize/center/list。app は対象アプリ名（省略時は最前面のウィンドウ）。メインディスプレイ前提。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["left", "right", "maximize", "center", "list"], "description": "配置/操作"},
                "app": {"type": "string", "description": "対象アプリ名（省略可＝最前面）"},
            },
            "required": ["action"],
        },
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
}


def run_tool(name: str, tool_input: dict) -> str:
    func = DISPATCH.get(name)
    if not func:
        return f"未知のツール: {name}"
    return func(**tool_input)
