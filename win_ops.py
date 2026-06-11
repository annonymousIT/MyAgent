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
    """主モニタのタスクバーを除いた作業領域 (x, y, w, h)。SystemParametersInfo(SPI_GETWORKAREA)。"""
    rect = wintypes.RECT()
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return 0, 0, 1920, 1040
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_MONITORINFOF_PRIMARY = 1


def _monitors() -> "list[tuple]":
    """全モニタの作業領域を左→右の順に返す。各要素 (x, y, w, h, is_primary)。"""
    mons: "list[tuple]" = []

    # コールバック型は Windows 専用（ctypes.WINFUNCTYPE）。ここ（Windows実行時）で定義する。
    proc_t = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
    )

    def _cb(hmon, _hdc, _lprc, _lparam):
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if user32.GetMonitorInfoW(wintypes.HMONITOR(hmon) if hasattr(wintypes, "HMONITOR") else hmon,
                                  ctypes.byref(mi)):
            w = mi.rcWork
            mons.append((w.left, w.top, w.right - w.left, w.bottom - w.top,
                         bool(mi.dwFlags & _MONITORINFOF_PRIMARY)))
        return True

    user32.EnumDisplayMonitors(None, None, proc_t(_cb), 0)
    mons.sort(key=lambda m: m[0])  # x 座標で左→右に並べる
    return mons


def _target_rect(monitor: str) -> "tuple[tuple, bool]":
    """配置先モニタの作業領域 (x,y,w,h) と「真の最大化を使えるか」を返す。

    monitor 未指定 → 主モニタ＝現在の画面で SW_MAXIMIZE が使える(True)。
    monitor 指定（左/右/番号）→ そのモニタ矩形に MoveWindow で合わせる(False)。
    """
    m = (monitor or "").strip().lower()
    if not m:
        return _work_area(), True
    mons = _monitors()
    if not mons:
        return _work_area(), True
    rects = [(x, y, w, h) for (x, y, w, h, _p) in mons]
    if m in ("left", "左", "ひだり", "leftmost"):
        return rects[0], False
    if m in ("right", "右", "みぎ", "rightmost"):
        return rects[-1], False
    if m in ("primary", "主", "メイン", "main"):
        for (x, y, w, h, p) in mons:
            if p:
                return (x, y, w, h), False
        return rects[0], False
    if m.isdigit():
        i = int(m) - 1
        return (rects[i] if 0 <= i < len(rects) else rects[0]), False
    return _work_area(), True


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


def _place(hwnd: int, action: str, rect: tuple, true_maximize: bool) -> None:
    """ウィンドウを rect（配置先モニタの作業領域）の左半分/右半分/最大化/中央に配置する。"""
    user32.ShowWindow(hwnd, SW_RESTORE)  # 最大化状態だと座標指定が効かないので一旦戻す
    x, y, w, h = rect
    if action == "maximize":
        if true_maximize:                       # 同一モニタ内ならOSの最大化（隙間なし）
            user32.ShowWindow(hwnd, SW_MAXIMIZE)
        else:                                   # 別モニタ指定はそのモニタの作業領域いっぱいに移動
            user32.MoveWindow(hwnd, x, y, w, h, True)
        return
    if action == "left":
        rx, ry, rw, rh = x, y, w // 2, h
    elif action == "right":
        rx, ry, rw, rh = x + w // 2, y, w - w // 2, h
    else:  # center
        rw, rh = int(w * 0.7), int(h * 0.8)
        rx, ry = x + (w - rw) // 2, y + (h - rh) // 2
    user32.MoveWindow(hwnd, rx, ry, rw, rh, True)


def manage_window(action: str, exe: str = "", monitor: str = "") -> "tuple[bool, str]":
    """ウィンドウ配置。exe 指定があればそのアプリの窓、無ければ最前面の窓を動かす。

    monitor: "" なら現在の画面（OS最大化が使える）。"left"/"right"/番号 なら、そのモニタの
    作業領域に対して action（左半分/右半分/最大化/中央）を適用する＝「左の画面にDiscord」が通る。
    起動直後は窓生成に時間差があるため、exe 指定時は数回リトライして待つ（Mac版と同趣旨）。
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
        rect, true_max = _target_rect(monitor)
        _place(hwnd, action, rect, true_max)
        return True, "ok"
    except Exception as e:  # ctypes 例外でも本体を止めない
        return False, f"{type(e).__name__}: {e}"


def monitor_count() -> int:
    """接続モニタ数（プロンプトで『画面は2枚』と伝えるため）。"""
    try:
        return len(_monitors())
    except Exception:
        return 1


# --------------------------------------------------------------------------
# システム操作（音量・モニタ・スリープ等）— nircmd 不要のネイティブ実装
# --------------------------------------------------------------------------
_VK = {"volume_down": 0xAE, "volume_up": 0xAF, "mute": 0xAD, "unmute": 0xAD,
       "media_playpause": 0xB3, "media_next": 0xB0, "media_prev": 0xB1}
_VOL_STEPS = 5  # 1回の「音量下げる/上げる」で送るキー回数（1回≈2%）


def _tap(vk: int) -> None:
    user32.keybd_event(vk, 0, 0, 0)   # down
    user32.keybd_event(vk, 0, 2, 0)   # up (KEYEVENTF_KEYUP)


def system_action(action: str) -> bool:
    """正規化済みアクションを実行。対応してなければ False（呼び出し側がconfig文字列へフォールバック）。"""
    if action in _VK:
        times = _VOL_STEPS if action in ("volume_down", "volume_up") else 1
        for _ in range(times):
            _tap(_VK[action])
        return True
    if action == "lock":
        user32.LockWorkStation()
        return True
    if action == "screenshot":
        # Win+PrtScn ＝ スクショを Pictures\Screenshots に自動保存
        VK_LWIN, VK_SNAPSHOT = 0x5B, 0x2C
        user32.keybd_event(VK_LWIN, 0, 0, 0)
        _tap(VK_SNAPSHOT)
        user32.keybd_event(VK_LWIN, 0, 2, 0)
        return True
    if action == "monitor_off":
        # 画面を省電力オフ（HWND_BROADCAST に WM_SYSCOMMAND/SC_MONITORPOWER, 2=オフ）
        user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)
        return True
    if action == "sleep":
        ctypes.windll.powrprof.SetSuspendState(0, 1, 0)
        return True
    if action in ("restart", "shutdown"):
        flag = "/r" if action == "restart" else "/s"
        subprocess.run(["shutdown", flag, "/t", "60"], check=False)
        return True
    return False
