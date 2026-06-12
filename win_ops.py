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
    user32.IsIconic.argtypes = [wintypes.HWND]            # 最小化判定（▽タグ）。restype は既定c_intでBOOL可
    user32.IsIconic.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]  # restore で前面化（「出して」）
    user32.SetForegroundWindow.restype = wintypes.BOOL
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
SW_MINIMIZE = 6
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


# ブラウザ本体ウィンドウのタイトル末尾。サイト/PWAを閉じる時にこれを巻き込むと全タブが死ぬので除外。
_BROWSER_SUFFIX = (" - google chrome", " - microsoft edge", " - mozilla firefox",
                   " - brave", " - opera", " - vivaldi")


def close_window_by_title(cands: "list[str]") -> bool:
    """タイトルに候補語を含む可視ウィンドウへ WM_CLOSE を送る（native/UWP/PWA 問わず閉じられる）。

    exe を持たない UWP/PWA も「窓」は持つので、これで優しく閉じられる（保存確認も出る＝taskkill /F より安全）。
    日本語2文字（電卓・写真等）も拾えるよう閾値は2。ただしブラウザ本体ウィンドウ（タブの集合）は、
    明示的にブラウザを閉じたい時以外は巻き込まない（サイトを閉じたつもりで全タブを消さない）。
    1つでも閉じたら True。
    """
    cl = [c.lower() for c in cands if c and len(c) >= 2]
    if not cl:
        return False

    def _scan() -> "list[int]":
        """タイトルに候補語を含む対象窓を列挙（ブラウザ本体は除外）。"""
        wants_browser = any(b in c for c in cl for b in ("chrome", "edge", "firefox", "brave", "opera"))
        hits: "list[int]" = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n == 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value.lower()
            if not wants_browser and title.endswith(_BROWSER_SUFFIX):
                return True  # ブラウザ本体は巻き込まない（PWA独立窓は接尾辞が付かないので対象に残る）
            if any(c in title for c in cl):
                hits.append(hwnd)
            return True

        user32.EnumWindows(_cb, 0)
        return hits

    hits = _scan()
    if not hits:
        return False
    # WM_CLOSE は PostMessage（非同期）。投げただけで成功を断定せず、実際に消えたか確認する。
    # 残れば（保存ダイアログ・PWAが無視等）もう一度投げ、それでも残れば False＝呼び出し側が正直に報告/taskkill。
    WM_CLOSE = 0x0010
    for h in hits:
        user32.PostMessageW(h, WM_CLOSE, 0, 0)
    time.sleep(0.4)
    if _scan():
        for h in _scan():
            user32.PostMessageW(h, WM_CLOSE, 0, 0)
        time.sleep(0.5)
    return not _scan()  # 対象窓が全部消えた時だけ True


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


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT)]


def _find_window_by_title(cands: "list[str]") -> "int | None":
    """タイトルに候補語を含む“本体”ウィンドウを返す（exe より信頼できる）。

    Discord は登録 exe がランチャー(Update.exe)、Spotify 等 UWP は exe 無しで exe 探索が当たらないが、
    タイトル（"… - Discord" / "Spotify Premium" / "GitHub … - Chrome"）は当たる。
    ただしアプリは同名のヘルパー窓（最小化・160x28等）も持つので、オーナー付き/ツール窓を除外し、
    “復元時のサイズが最大”の窓＝本体を選ぶ。
    """
    cl = [c.lower() for c in cands if c and len(c) >= 2]
    if not cl:
        return None
    best = {"hwnd": None, "area": -1}
    GW_OWNER, GWL_EXSTYLE, WS_EX_TOOLWINDOW = 4, -20, 0x80

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if not any(c in buf.value.lower() for c in cl):
            return True
        if user32.GetWindow(hwnd, GW_OWNER):                  # オーナー付き＝補助窓
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:  # ツール窓
            return True
        wp = _WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
        area = 0
        if user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):  # 復元時サイズ＝本体らしさ
            r = wp.rcNormalPosition
            area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
        if area > best["area"]:
            best["hwnd"], best["area"] = hwnd, area
        return True

    user32.EnumWindows(_cb, 0)
    return best["hwnd"]


def manage_window(action: str, exe: str = "", monitor: str = "", titles=None) -> "tuple[bool, str]":
    """ウィンドウ配置。titles（タイトル候補）→exe の順で対象窓を探す。アプリ指定がある（titles/exe）
    のに窓が見つからなければ、前面の別窓を動かさず**正直に失敗を返す**（誤って関係ない窓を動かさない）。
    titles も exe も空のときだけ最前面の窓を動かす（「これを右に」等）。

    monitor: "" なら現在の画面（OS最大化）。"left"/"right"/番号 ならそのモニタ基準で action を適用。
    """
    hwnd = None
    if titles or exe:
        for _ in range(5):  # 窓生成待ち（起動直後のラグ・最大 ~2秒）
            hwnd = _find_window_by_title(titles) if titles else None
            if not hwnd and exe:
                hwnd = _find_window_by_exe(exe)
            if hwnd:
                break
            time.sleep(0.4)
        if not hwnd:
            return False, "対象のウィンドウが見つかりません（起動していない可能性）。"
    else:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False, "前面のウィンドウが取得できませんでした。"
    try:
        # 最小化／復元は座標計算が不要（しまう・出すだけ）。配置系（left/right/maximize/center）より先に処理。
        if action == "minimize":
            user32.ShowWindow(hwnd, SW_MINIMIZE)
            return True, "ok"
        if action == "restore":
            user32.ShowWindow(hwnd, SW_RESTORE)   # 最小化/最大化どちらからも元のサイズへ
            user32.SetForegroundWindow(hwnd)      # 前面へ（「出して」=見える状態に）
            return True, "ok"
        rect, true_max = _target_rect(monitor)
        _place(hwnd, action, rect, true_max)
        # モニタ指定時は「載ったつもり」で終わらせない：実際にそのモニタへ移ったか確認し、
        # 外れていたら一度だけやり直す（Chrome/最大化窓が MoveWindow を無視して元画面に残る事故対策）。
        if monitor and not _window_on_rect(hwnd, rect):
            time.sleep(0.25)
            _place(hwnd, action, rect, true_max)
            if not _window_on_rect(hwnd, rect):
                return False, "指定モニタへ移動できませんでした（ウィンドウが追従しません）。"
        return True, "ok"
    except Exception as e:  # ctypes 例外でも本体を止めない
        return False, f"{type(e).__name__}: {e}"


def _window_on_rect(hwnd: int, rect: tuple) -> bool:
    """ウィンドウの中心が rect（配置先モニタの作業領域 x,y,w,h）に入っているか＝そのモニタに載ったか。"""
    x, y, w, h = rect
    r = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return True  # 取得不能なら判定を諦めて成功扱い（誤った失敗報告を避ける）
    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    return x <= cx <= x + w and y <= cy <= y + h


def monitor_count() -> int:
    """接続モニタ数（プロンプトで『画面は2枚』と伝えるため）。"""
    try:
        return len(_monitors())
    except Exception:
        return 1


_MONITOR_DEFAULTTONEAREST = 2
_SHELL_CLASSES = {"Progman", "WorkerW"}  # デスクトップ自身（常に画面いっぱい）は全画面扱いしない

# ウィンドウ一覧で無視するタイトル（システムUI・自分自身）
_WIN_TITLE_IGNORE = ("Program Manager", "Windows 入力エクスペリエンス", "NVIDIA GeForce Overlay",
                     "PowerToys", "tk", "MyAgent", "Windows PowerShell", "コマンド プロンプト")


def windows_overview(max_n: int = 12, title_len: int = 20) -> "list[tuple[int, str]]":
    """可視ウィンドウを (モニタ番号(左から1始まり), 短縮タイトル) で返す（⑦ ウィンドウ配置の把握）。

    LLM に「どの画面に何が開いているか」を渡し、『整理して』『右のやつ左へ』等の柔軟な指示を
    可能にするための材料。トークン節約のためタイトルは短縮し件数も絞る。
    """
    mons = _monitors()
    xs = [m[0] for m in mons]

    out: "list[tuple[int, str]]" = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        if len(out) >= max_n or not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if any(ig in title for ig in _WIN_TITLE_IGNORE):
            return True
        hmon = user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        idx = 1
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)) and xs:
            try:
                idx = xs.index(mi.rcWork.left) + 1
            except ValueError:
                idx = 1
        t = title if len(title) <= title_len else title[:title_len] + "…"
        if user32.IsIconic(hwnd):   # 最小化中は頭に▽（LLMが「出して/再配置」を判断できるよう・凡例は静的側）
            t = "▽" + t
        out.append((idx, t))
        return True

    user32.EnumWindows(_cb, 0)
    return out


def foreground_is_fullscreen() -> bool:
    """前面ウィンドウが、その載っているモニタを完全に覆っている（=全画面ゲーム/動画）か。

    orb がゲームの上に浮いて邪魔をしないための判定。タスクバーを含むモニタ全域（rcMonitor）
    との一致で見るので、通常の「最大化」（作業領域まで）は全画面とみなさない。
    """
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in _SHELL_CLASSES:
        return False
    r = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return False
    hmon = user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return False
    m = mi.rcMonitor
    return r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom


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
