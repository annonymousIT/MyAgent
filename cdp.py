"""Chrome DevTools Protocol（CDP）でタブ単位の管理をする（ADR-0040 / ④）。

Chrome を `--remote-debugging-port=9222` 付きで起動すると、HTTP でタブの列挙・作成・
クローズ・前面化ができる。見た目・使い心地は普通の Chrome と同じ。

戦略（壊さない・徐々に移行）:
- エージェントがURLを開く時、CDP が生きていれば CDP でタブを開く（＝後で確実に閉じられる）
- CDP が無く Chrome も未起動なら、デバッグポート付きで Chrome を起動して開く（次回から管理可能に）
- CDP が無く Chrome が既に起動中なら、従来どおり既定ブラウザで開く（閉じる管理は不可・正直に劣化）

標準ライブラリのみ（urllib）。すべて短いタイムアウトで、失敗しても呼び出し側を止めない。
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

PORT = int(os.environ.get("CDP_PORT", "9222"))
BASE = f"http://127.0.0.1:{PORT}"

_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
)


def _get(path: str, timeout: float = 0.6, method: str = "GET") -> "object | None":
    try:
        req = urllib.request.Request(BASE + path, method=method)
        raw = urllib.request.urlopen(req, timeout=timeout).read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return None


def available() -> bool:
    """CDP（デバッグポート付き Chrome）が生きているか。"""
    return _get("/json/version", timeout=0.4) is not None


def tabs() -> "list[dict]":
    """開いているタブ一覧 [{id, title, url}]。CDP が無ければ空。"""
    data = _get("/json/list")
    if not isinstance(data, list):
        return []
    return [{"id": t.get("id", ""), "title": t.get("title", ""), "url": t.get("url", "")}
            for t in data if t.get("type") == "page"]


def open_tab(url: str) -> bool:
    """新しいタブで URL を開く（新しめの Chrome は PUT 必須・古いのは GET なので両方試す）。"""
    q = "/json/new?" + urllib.parse.quote(url, safe=":/?&=%#+")
    return _get(q, method="PUT") is not None or _get(q) is not None


def close_by_url(substrings: "list[str]") -> int:
    """URL に部分一致するタブを閉じる。閉じた数を返す（ephemeral の後片付け用）。"""
    subs = [s for s in substrings if s]
    if not subs:
        return 0
    n = 0
    for t in tabs():
        if any(s in t["url"] for s in subs):
            if _get(f"/json/close/{t['id']}") is not None:
                n += 1
    return n


def close_by_title(substrings: "list[str]") -> int:
    """タイトルに部分一致するタブを閉じる（「YouTube閉じて」のサイト名向け）。"""
    subs = [s.lower() for s in substrings if s and len(s) >= 2]
    if not subs:
        return 0
    n = 0
    for t in tabs():
        if any(s in t["title"].lower() or s in t["url"].lower() for s in subs):
            if _get(f"/json/close/{t['id']}") is not None:
                n += 1
    return n


def chrome_path() -> "str | None":
    for p in _CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def launch_chrome_with_cdp(url: str = "") -> bool:
    """デバッグポート付きで Chrome を起動する（以後タブ管理ができるようになる）。"""
    exe = chrome_path()
    if not exe:
        return False
    args = [exe, f"--remote-debugging-port={PORT}"]
    if url:
        args.append(url)
    try:
        subprocess.Popen(args)
        return True
    except Exception:
        return False
