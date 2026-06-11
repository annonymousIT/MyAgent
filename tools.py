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

import json
import platform
import subprocess
import unicodedata
import urllib.parse
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
    try:
        if IS_MAC:
            subprocess.Popen(["open", "-a", target])
        else:
            subprocess.Popen([target])
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


def web_search(query: str) -> str:
    """登録ソースに無い情報を『届ける』ための汎用Web検索（Tier2 / ADR-0018）。

    天気・株価・ニュースなど、知りたい情報をブラウザの検索結果として開く。
    捏造防止：答えを想像で言わず、必ず実ソース（検索結果）を開く。Step3で読み上げまで繋げる。
    """
    if not query or not query.strip():
        return "検索語が空です。"
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
    webbrowser.open(url)
    return f"「{query}」を検索しました。"


def open_url(url: str) -> str:
    """任意のURLをブラウザで開く（Tier2 / ADR-0018）。明確なURLが分かっているときに使う。"""
    if not url or not url.strip():
        return "URLが空です。"
    target = url.strip()
    if not target.startswith(("http://", "https://")):
        target = "https://" + target
    webbrowser.open(target)
    return f"{target} を開きました。"


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
]

# ツール名 → 実関数
DISPATCH = {
    "open_site": open_site,
    "launch_app": launch_app,
    "run_system": run_system,
    "web_search": web_search,
    "open_url": open_url,
}


def run_tool(name: str, tool_input: dict) -> str:
    func = DISPATCH.get(name)
    if not func:
        return f"未知のツール: {name}"
    return func(**tool_input)
