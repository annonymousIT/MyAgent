"""操作（“手”）の実装。OS（Windows / Mac）を自動判定し、対応する設定表から実体を引く。

設定表は OS ごとに分離（config_win.json / config_mac.json）。
コード本体はOS差分を吸収するだけで、URL・アプリ・コマンドの実体は一切ベタ書きしない。
"""

import json
import platform
import subprocess
import webbrowser
from pathlib import Path

IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"

_CONFIG_NAME = "config_mac.json" if IS_MAC else "config_win.json"
CONFIG_PATH = Path(__file__).parent / _CONFIG_NAME


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def open_site(name: str) -> str:
    """名前→URL表からURLを引いてブラウザで開く（OS共通）。"""
    sites = load_config().get("sites", {})
    url = sites.get(name)
    if not url:
        return f"『{name}』に対応するサイトが {_CONFIG_NAME} にありません。"
    if url.startswith("https://("):  # まだ書き換えてないプレースホルダ
        return f"『{name}』のURLが未設定です（{_CONFIG_NAME} を書き換えてください）。"
    webbrowser.open(url)
    return f"{name} を開きました（{url}）。"


def launch_app(name: str) -> str:
    """名前→アプリ表から引いて起動する。Windowsはexeパス、Macはアプリ名を `open -a` で起動。"""
    apps = load_config().get("apps", {})
    target = apps.get(name)
    if not target:
        return f"『{name}』に対応するアプリが {_CONFIG_NAME} にありません。"
    if "(" in target:  # まだ書き換えてないプレースホルダ
        return f"『{name}』のパスが未設定です（{_CONFIG_NAME} を書き換えてください）。"
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

    return f"『{name}』に対応するシステムコマンドが {_CONFIG_NAME} にありません。"


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
        "description": "PCのアプリを起動する。引数 name は設定の apps 表のキー（例: ばろ, ディスコード, 電話）。表記ゆれは近いキーに寄せて解釈する。",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "起動するアプリの名前"}},
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
]

# ツール名 → 実関数
DISPATCH = {
    "open_site": open_site,
    "launch_app": launch_app,
    "run_system": run_system,
}


def run_tool(name: str, tool_input: dict) -> str:
    func = DISPATCH.get(name)
    if not func:
        return f"未知のツール: {name}"
    return func(**tool_input)
