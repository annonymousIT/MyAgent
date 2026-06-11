"""セットアップ：インストール済みアプリを自動スキャン → Claude で分類 → config.auto.json を書き出す。

フレームワーク化 step1（ADR-0011 / 0012 / 0016）。
身近な人に渡せるよう、各自のアプリ一覧・カテゴリ・日本語の呼び名（略語含む）を
手で書かずに自動生成する。誤りは config.user.json で上書きする（2層・user優先）。

使い方:
    source .env && source .venv/bin/activate && python setup.py

Mac専用（PWA/native を open -a で扱える）。WindowsはTODOスタブ。
"""

from __future__ import annotations

import json
import platform
import re
from pathlib import Path

import anthropic

import core

IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"

BASE = Path(__file__).parent
AUTO_CONFIG_PATH = BASE / "config.auto.json"

# 分類に使うモデル（安価・一度きり）
CLASSIFY_MODEL = "claude-haiku-4-5"

# 1回のAPI呼び出しで分類するアプリ数（プロンプトが膨らみすぎないよう分割）
CHUNK_SIZE = 30

# PWA（Chrome PWA）が置かれるディレクトリ名。ここ配下なら kind="pwa"
PWA_DIR_NAME = "Chrome Apps.localized"

# native アプリのスキャン対象（~ は展開する）
NATIVE_DIRS = [
    "/Applications",
    "/System/Applications",
    "~/Applications",
]


# --------------------------------------------------------------------------
# 1. スキャン
# --------------------------------------------------------------------------
def scan_apps() -> list[dict]:
    """インストール済みアプリ（PWA + native）をスキャンして一覧化する。

    返り値の各要素: {"name", "target", "kind", "path"}
    - name   : 表示名（.app を除いた basename）
    - target : open -a 用の名前（= .app を除いた basename。pwa/native 共通）
    - kind   : "pwa"（Chrome Apps.localized 配下） / "native"
    - path   : フルパス（.app）
    重複（同名）は最初に見つかったものを採用して排除する。
    """
    if not IS_MAC:
        raise RuntimeError("scan_apps は現状 Mac 専用です。")

    found: dict[str, dict] = {}  # name -> entry（重複排除のキーは表示名）

    # PWA: ~/Applications/Chrome Apps.localized/*.app
    pwa_dir = Path("~/Applications").expanduser() / PWA_DIR_NAME
    if pwa_dir.is_dir():
        for app in sorted(pwa_dir.glob("*.app")):
            _add_app(found, app, kind="pwa")

    # native: /Applications, /System/Applications, ~/Applications
    for raw in NATIVE_DIRS:
        d = Path(raw).expanduser()
        if not d.is_dir():
            continue
        for app in sorted(d.glob("*.app")):
            _add_app(found, app, kind="native")

    return list(found.values())


def _add_app(found: dict[str, dict], app_path: Path, kind: str) -> None:
    """スキャン結果を1件登録（同名は既存を優先して重複排除）。"""
    name = app_path.stem  # .app を除いた basename
    if name in found:
        return
    found[name] = {
        "name": name,
        "target": name,  # open -a はアプリ名（.app basename）で起動できる
        "kind": kind,
        "path": str(app_path),
    }


# --------------------------------------------------------------------------
# 2. 分類（Claude で category + 日本語エイリアス生成）
# --------------------------------------------------------------------------
_PROMPT_HEAD = """あなたはMac用音声エージェントのセットアップ補助です。
以下はユーザーのMacにインストール済みのアプリ名の一覧です。
各アプリについて、次の2つを生成してJSONで返してください。

1. category: アプリの用途カテゴリ。次のいずれか1語を選ぶ:
   動画 / 通話 / メール / 勉強 / 音楽 / ブラウザ / カレンダー / 仕事 / ゲーム / 開発 / ユーティリティ / その他
2. aliases: そのアプリを日本語の話し言葉で指すときの呼び名・略語の配列（0〜4個）。
   - ひらがな/カタカナの口語、よくある略語を入れる。
   - 例: YouTube -> ["ようつべ","ユーチューブ"], Discord -> ["ディスコ","でぃす"],
     Google カレンダー -> ["カレンダー","予定"], Gmail -> ["メール","じーめーる"]
   - 該当する自然な呼び名が無ければ空配列 [] でよい。アプリ名そのもの（英字表記）は入れない。

出力は必ず次の形式のJSONのみ（説明文・コードフェンスは禁止）:
{"results": [{"name": "<入力のnameと完全一致>", "category": "...", "aliases": ["...", "..."]}, ...]}

アプリ一覧（name のみ）:
"""


def classify_apps(client: anthropic.Anthropic, apps: list[dict]) -> dict[str, dict]:
    """アプリ一覧を Claude に渡し、name -> {category, aliases} を得る。

    CHUNK_SIZE 件ずつ分割して呼び出す。パース失敗時はそのチャンクを
    （カテゴリ「その他」・エイリアス空で）フォールバックして落とさない。
    """
    out: dict[str, dict] = {}
    chunks = [apps[i : i + CHUNK_SIZE] for i in range(0, len(apps), CHUNK_SIZE)]
    for idx, chunk in enumerate(chunks, 1):
        names = [a["name"] for a in chunk]
        print(f"  分類中... チャンク {idx}/{len(chunks)}（{len(names)}件）")
        parsed = _classify_chunk(client, names)
        for name in names:
            info = parsed.get(name)
            if info is None:
                out[name] = {"category": "その他", "aliases": []}
            else:
                out[name] = info
    return out


def _classify_chunk(
    client: anthropic.Anthropic, names: list[str], retries: int = 2
) -> dict[str, dict]:
    """1チャンクを分類。リトライ＋サニタイズでパース失敗に頑健にする。"""
    prompt = _PROMPT_HEAD + "\n".join(f"- {n}" for n in names)
    for attempt in range(retries + 1):
        try:
            resp = client.messages.create(
                model=CLASSIFY_MODEL,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            results = _parse_results(text)
            if results:
                return results
        except Exception as e:  # API/パース双方をまとめてリトライ対象に
            print(f"    （リトライ {attempt + 1}：{type(e).__name__}: {e}）")
    print("    （分類に失敗。このチャンクはフォールバックします）")
    return {}


def _parse_results(text: str) -> dict[str, dict]:
    """LLM出力から {"results": [...]} を取り出し name->info の辞書に整える。

    コードフェンスや前後の雑文が混ざっても、最初の { から最後の } を抜き出して
    JSONとして読む。各要素は category/aliases を欠いていても安全に補完する。
    """
    obj = _extract_json(text)
    if not isinstance(obj, dict):
        return {}
    results = obj.get("results")
    if not isinstance(results, list):
        return {}

    valid_categories = {
        "動画", "通話", "メール", "勉強", "音楽", "ブラウザ",
        "カレンダー", "仕事", "ゲーム", "開発", "ユーティリティ", "その他",
    }
    out: dict[str, dict] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        category = item.get("category")
        if category not in valid_categories:
            category = "その他"
        aliases = item.get("aliases")
        if not isinstance(aliases, list):
            aliases = []
        # 文字列のみ・空除去・重複除去
        clean_aliases: list[str] = []
        for a in aliases:
            if isinstance(a, str) and a.strip() and a.strip() not in clean_aliases:
                clean_aliases.append(a.strip())
        out[name] = {"category": category, "aliases": clean_aliases}
    return out


def _extract_json(text: str):
    """雑多なテキストから最初のJSONオブジェクトを抽出してパースする。"""
    # コードフェンス除去
    text = re.sub(r"```(?:json)?", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 最初の { 〜 最後の } を切り出して再挑戦
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        snippet = text[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            return None
    return None


# --------------------------------------------------------------------------
# 3. 書き出し
# --------------------------------------------------------------------------
def build_auto_config(apps: list[dict], classified: dict[str, dict]) -> dict:
    """スキャン結果＋分類結果を config.auto.json の構造に組み立てる。"""
    out_apps: dict[str, dict] = {}
    for a in apps:
        info = classified.get(a["name"], {"category": "その他", "aliases": []})
        out_apps[a["name"]] = {
            "target": a["target"],
            "kind": a["kind"],
            "path": a["path"],
            "category": info.get("category", "その他"),
            "aliases": info.get("aliases", []),
        }
    return {"apps": out_apps}


def write_auto_config(config: dict) -> None:
    AUTO_CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------
# エントリポイント
# --------------------------------------------------------------------------
def main() -> None:
    if IS_WINDOWS:
        # TODO(win): Windows のアプリスキャンは未実装。
        #   スタートメニューの .lnk（%APPDATA%\Microsoft\Windows\Start Menu\Programs）や
        #   レジストリのアンインストール情報を走査して同等の一覧を作る想定。
        print("Windows 版のアプリスキャンは未実装です（TODO）。")
        print("現状このセットアップは Mac 専用です。手動で config.user.json を編集してください。")
        return
    if not IS_MAC:
        print(f"未対応のOSです（{platform.system()}）。Mac でのみ動作します。")
        return

    print("インストール済みアプリをスキャンしています...")
    apps = scan_apps()
    pwa_n = sum(1 for a in apps if a["kind"] == "pwa")
    native_n = sum(1 for a in apps if a["kind"] == "native")
    print(f"  スキャン完了：{len(apps)} 件（PWA {pwa_n} / native {native_n}）")

    if not apps:
        print("アプリが見つかりませんでした。終了します。")
        return

    print("Claude でカテゴリ・呼び名を分類しています...")
    client = anthropic.Anthropic(api_key=_require_api_key())
    classified = classify_apps(client, apps)

    config = build_auto_config(apps, classified)
    write_auto_config(config)

    classified_n = sum(
        1 for v in config["apps"].values() if v["category"] != "その他" or v["aliases"]
    )
    print(f"\n✅ {AUTO_CONFIG_PATH.name} を書き出しました。")
    print(f"   アプリ {len(config['apps'])} 件中、{classified_n} 件にカテゴリ/呼び名を付与しました。")
    print("   誤りは config.user.json の apps / overrides で上書きできます（user層優先）。")


def _require_api_key() -> str:
    key = core.load_api_key()
    if not key:
        raise SystemExit(
            "ANTHROPIC_API_KEY が見つかりません（source .env するか環境変数を設定してください）。"
        )
    return key


if __name__ == "__main__":
    main()
