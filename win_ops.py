"""Windows固有の操作（ウィンドウ配置・起動中把握・終了）。

tools.py が Windows のとき、ここに委譲する。Mac の AppleScript 経路に対応する Windows 実装。
- ウィンドウ配置 : ctypes(user32) で SetWindowPos / ShowWindow（Macの純正タイル相当）
- 起動中把握    : tasklist（Macの System Events / pgrep 相当）
- 終了          : taskkill（Macの quit 相当）
外部依存なし（標準ライブラリのみ）。Mac から import されても安全なよう、windll は Windows でのみ束縛する。
"""

from __future__ import annotations

import ctypes
import platform
import subprocess
import time
from ctypes import wintypes

IS_WINDOWS = platform.system() == "Windows"

# windll は Windows にしか無い。Mac で import されても壊れないよう Windows でだけ束縛する。
if IS_WINDOWS:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # 64bit でハンドルが切り詰められないよう、関わる関数の型を明示する。
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.MoveWindow.argtypes = [
        wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

SW_MAXIMIZE = 3
SW_RESTORE = 9
SPI_GETWORKAREA = 0x0030
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


# --------------------------------------------------------------------------
# 起動中プロセス（tasklist）— pgrep / System Events 相当
# --------------------------------------------------------------------------
def _running_exes() -> "set[str]":
    """いま動いているプロセスの exe 名（小文字）の集合。tasklist を CSV で読む。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=8, encoding="utf-8", errors="ignore",
        )
    except Exception:
        return set()
    exes = set()
    for line in out.stdout.splitlines():
        if line.startswith('"'):
            name = line.split('","', 1)[0].strip('"')  # 先頭フィールド = Image Name
            if name:
                exes.add(name.lower())
    return exes


# 起動中一覧から除くノイズ（シェル・ターミナル・開発ツール・自分自身）。Mac の _RUNNING_IGNORE 相当。
_RUNNING_IGNORE_EXE = {
    "powershell.exe", "pwsh.exe", "cmd.exe", "conhost.exe", "windowsterminal.exe",
    "explorer.exe", "python.exe", "pythonw.exe", "py.exe", "code.exe",
}


def running_apps(cfg: dict) -> "list[str]":
    """config の apps のうち、いま起動中（exe がプロセス一覧にある）の表示名を返す。

    シェル・ターミナル・自分自身（python）等のノイズ exe は除外する。
    """
    running = _running_exes()
    res = []
    for name, entry in cfg.get("apps", {}).items():
        if isinstance(entry, dict):
            exe = (entry.get("exe") or "").lower()
            if exe and exe in running and exe not in _RUNNING_IGNORE_EXE:
                res.append(name)
    return res


def app_is_running(exe: str) -> bool:
    """指定 exe 名（例 Discord.exe）が起動中か。重複起動の判定に使う。"""
    if not exe:
        return False
    return exe.lower() in _running_exes()


def close_exe(exe: str) -> bool:
    """taskkill で指定 exe を終了する（Mac の quit 相当）。成功で True。"""
    if not exe:
        return False
    try:
        r = subprocess.run(
            ["taskkill", "/IM", exe, "/F"], capture_output=True, text=True, timeout=8
        )
        return r.returncode == 0
    except Exception:
        return False


# --------------------------------------------------------------------------
# ウィンドウ配置（ctypes user32）— Mac の純正タイル相当
# --------------------------------------------------------------------------
def _work_area() -> "tuple[int, int, int, int]":
    """タスクバーを除いた作業領域 (x, y, w, h)。SystemParametersInfo(SPI_GETWORKAREA)。"""
    rect = wintypes.RECT()
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return 0, 0, 1920, 1040
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _exe_of_pid(pid: int) -> str:
    """PID から exe 名（basename・小文字）を得る。権限不要の QueryFullProcessImageName。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.split("\\")[-1].lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def _find_window_by_exe(exe: str) -> "int | None":
    """指定 exe が持つ、可視でタイトルのあるトップレベルウィンドウを1つ探す。"""
    exe = (exe or "").lower()
    if not exe:
        return None
    found: "list[int]" = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) == 0:  # タイトル無し＝ツール窓等は除外
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _exe_of_pid(pid.value) == exe:
            found.append(hwnd)
            return False  # 見つけたら走査終了
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else None


def _place(hwnd: int, action: str) -> None:
    """ウィンドウを左半分 / 右半分 / 最大化 / 中央 に配置する。"""
    user32.ShowWindow(hwnd, SW_RESTORE)  # 最大化状態だと座標指定が効かないので一旦戻す
    if action == "maximize":
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
        return
    x, y, w, h = _work_area()
    if action == "left":
        rx, ry, rw, rh = x, y, w // 2, h
    elif action == "right":
        rx, ry, rw, rh = x + w // 2, y, w - w // 2, h
    else:  # center
        rw, rh = int(w * 0.7), int(h * 0.8)
        rx, ry = x + (w - rw) // 2, y + (h - rh) // 2
    user32.MoveWindow(hwnd, rx, ry, rw, rh, True)


def manage_window(action: str, exe: str = "") -> "tuple[bool, str]":
    """ウィンドウ配置。exe 指定があればそのアプリの窓、無ければ最前面の窓を動かす。

    返り値 (成功?, メッセージ)。tools.manage_window から委譲される。
    起動直後はアプリが窓を生成するまで時間差があるため、exe 指定時は数回リトライして待つ
    （Win11 のメモ帳など窓生成が遅いアプリ対策。Mac 版のリトライと同趣旨）。
    """
    hwnd = None
    if exe:
        for _ in range(6):  # 窓生成待ち（最大 ~3秒）
            hwnd = _find_window_by_exe(exe)
            if hwnd:
                break
            time.sleep(0.5)
    else:
        hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False, "対象のウィンドウが見つかりませんでした（起動済みか確認してください）。"
    try:
        _place(hwnd, action)
        return True, "ok"
    except Exception as e:  # ctypes 例外でも本体を止めない
        return False, f"{type(e).__name__}: {e}"
