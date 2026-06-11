"""デスクトップに浮く、ホワンホワン脈打つ丸いエージェント（presence orb）。

「私、ここにいますよ」を示すデスクトップ常駐ギミック。
- ふわっと拡縮＋明滅して脈打つ（ホワンホワン）
- ドラッグで好きな位置へ移動
- クリックで設定画面（音声ON/OFF・声の種類・ピッチ/速度・声テスト・今月の予算）

tkinter のみ（Python標準同梱・追加依存なし）。Windows は透明背景で丸く浮く。
起動: python overlay.py
"""

from __future__ import annotations

import math
import platform
import threading
import tkinter as tk
from tkinter import ttk

import settings

IS_WINDOWS = platform.system() == "Windows"

CHROMA = "#10121a"       # 透明にする背景色（この色のピクセルが透ける・Windows）
ORB_CORE = (120, 205, 255)   # orb の芯の色（やわらかい水色）
SIZE = 170               # ウィンドウの一辺(px)。orb＋脈動の余白
CENTER = SIZE // 2
BASE_R = 42              # orb の基準半径
PULSE = 7                # 脈動の振幅(px)

# VOICEVOX のよく使う話者プリセット（名前 → speaker id）
SPEAKERS = {
    "ずんだもん（ノーマル）": "3",
    "四国めたん（ノーマル）": "2",
    "春日部つむぎ": "8",
    "雨晴はう": "10",
    "冥鳴ひまり": "14",
    "玄野武宏": "11",
    "青山龍星": "13",
}


def _lerp(a: tuple, b: tuple, t: float) -> str:
    """2色(RGB)を t(0..1) で補間して #rrggbb を返す。"""
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


CHROMA_RGB = (16, 18, 26)


class Orb:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.overrideredirect(True)              # タイトルバー無し
        self.root.wm_attributes("-topmost", True)     # 常に最前面
        try:
            self.root.wm_attributes("-transparentcolor", CHROMA)  # CHROMA色を透明に（Windows）
        except tk.TclError:
            pass
        self.root.config(bg=CHROMA)

        # 右下あたりに初期配置
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{SIZE}x{SIZE}+{sw - SIZE - 60}+{sh - SIZE - 120}")

        self.canvas = tk.Canvas(self.root, width=SIZE, height=SIZE,
                                bg=CHROMA, highlightthickness=0)
        self.canvas.pack()

        # ドラッグ↔クリックの判定
        self._press = None
        self._moved = False
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", lambda e: self._quit())  # 右クリックで終了

        self._phase = 0.0
        self._settings_win = None
        self._animate()

    # ---- アニメーション（ホワンホワン）----
    def _animate(self) -> None:
        self._phase += 0.08
        s = (math.sin(self._phase) + 1) / 2          # 0..1 でゆっくり往復
        r = BASE_R + PULSE * s                        # 半径が膨らむ/縮む
        self.canvas.delete("all")
        # 外側のハロー（外ほど CHROMA に溶けて消える＝やわらかい光）
        rings = 5
        for i in range(rings, 0, -1):
            t = i / rings
            rr = r + t * 26
            col = _lerp(ORB_CORE, CHROMA_RGB, t)      # 外側ほど透明色へ
            self.canvas.create_oval(CENTER - rr, CENTER - rr, CENTER + rr, CENTER + rr,
                                    fill=col, outline="")
        # 芯（明滅：脈動に合わせて少し明るく）
        core = _lerp(ORB_CORE, (255, 255, 255), 0.15 + 0.25 * s)
        self.canvas.create_oval(CENTER - r, CENTER - r, CENTER + r, CENTER + r,
                                fill=core, outline="")
        # ハイライト（つやの点）
        hr = r * 0.28
        hx, hy = CENTER - r * 0.32, CENTER - r * 0.34
        self.canvas.create_oval(hx - hr, hy - hr, hx + hr, hy + hr,
                                fill="#ffffff", outline="")
        self.root.after(40, self._animate)

    # ---- ドラッグ／クリック ----
    def _on_press(self, e) -> None:
        self._press = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())
        self._moved = False

    def _on_drag(self, e) -> None:
        if not self._press:
            return
        dx, dy = e.x_root - self._press[0], e.y_root - self._press[1]
        if abs(dx) > 4 or abs(dy) > 4:
            self._moved = True
        self.root.geometry(f"+{self._press[2] + dx}+{self._press[3] + dy}")

    def _on_release(self, e) -> None:
        if not self._moved:        # 動かさずに離した＝クリック → 設定
            self._open_settings()
        self._press = None

    def _quit(self) -> None:
        self.root.destroy()

    # ---- 設定画面 ----
    def _open_settings(self) -> None:
        if self._settings_win and tk.Toplevel.winfo_exists(self._settings_win):
            self._settings_win.lift()
            return
        cfg = settings.load()
        win = tk.Toplevel(self.root)
        self._settings_win = win
        win.title("MyAgent 設定")
        win.config(padx=18, pady=16)
        win.wm_attributes("-topmost", True)

        ttk.Label(win, text="🫧 MyAgent", font=("", 14, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(win, text="マスター専属PCパートナーの設定", foreground="#666").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))

        # 音声 ON/OFF
        voice_var = tk.BooleanVar(value=bool(cfg.get("voice_enabled", True)))
        ttk.Checkbutton(win, text="声を出す", variable=voice_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=4)

        # 声の種類
        ttk.Label(win, text="声（VOICEVOX）").grid(row=3, column=0, sticky="w", pady=4)
        id2name = {v: k for k, v in SPEAKERS.items()}
        cur_name = id2name.get(str(cfg.get("voicevox_speaker", "8")), "春日部つむぎ")
        name_var = tk.StringVar(value=cur_name)
        ttk.OptionMenu(win, name_var, cur_name, *SPEAKERS.keys()).grid(row=3, column=1, sticky="ew", pady=4)

        # ピッチ
        ttk.Label(win, text="声の高さ").grid(row=4, column=0, sticky="w", pady=4)
        pitch_var = tk.DoubleVar(value=float(cfg.get("voicevox_pitch", -0.085)))
        ttk.Scale(win, from_=-0.15, to=0.15, variable=pitch_var, length=160).grid(row=4, column=1, sticky="ew", pady=4)

        # 速度
        ttk.Label(win, text="話す速さ").grid(row=5, column=0, sticky="w", pady=4)
        speed_var = tk.DoubleVar(value=float(cfg.get("voicevox_speed", 0.95)))
        ttk.Scale(win, from_=0.7, to=1.3, variable=speed_var, length=160).grid(row=5, column=1, sticky="ew", pady=4)

        # 今月の予算
        budget_txt = _budget_line()
        ttk.Label(win, text=budget_txt, foreground="#888").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(12, 8))

        def _collect() -> dict:
            return {
                "voice_enabled": voice_var.get(),
                "voicevox_speaker": SPEAKERS.get(name_var.get(), "8"),
                "voicevox_pitch": round(pitch_var.get(), 3),
                "voicevox_speed": round(speed_var.get(), 2),
            }

        def _save() -> None:
            settings.save(_collect())
            win.destroy()
            self._settings_win = None

        def _test() -> None:
            settings.save(_collect())  # 今の値で試聴
            threading.Thread(
                target=lambda: _safe_speak("はい、マスター。ちゃんと聞こえていますか？"),
                daemon=True,
            ).start()

        btns = ttk.Frame(win)
        btns.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(btns, text="🔊 声をテスト", command=_test).pack(side="left")
        ttk.Button(btns, text="保存", command=_save).pack(side="right")
        ttk.Button(btns, text="閉じる", command=win.destroy).pack(side="right", padx=6)

        win.columnconfigure(1, weight=1)

    def run(self) -> None:
        self.root.mainloop()


def _safe_speak(text: str) -> None:
    try:
        import speak
        speak.speak(text)
    except Exception:
        pass


def _budget_line() -> str:
    """今月の予算状況を1行で（core が読めない環境でも落とさない）。"""
    try:
        import core
        st = core.budget_status()
        return f"今月のコスト：¥{st['month']} / {st['budget']}（残 ¥{st['remaining']}）"
    except Exception:
        return "今月のコスト：（記録なし）"


def main() -> None:
    Orb().run()


if __name__ == "__main__":
    main()
