"""デスクトップに浮く、ホワンホワン脈打つ丸いエージェント（presence orb）。

「私、ここにいますよ」を示すデスクトップ常駐ギミック。
- ふわっと拡縮＋明滅して脈打つ（ホワンホワン）
- ドラッグで好きな位置へ移動
- クリックで設定画面（音声ON/OFF・声の種類・ピッチ/速度・声テスト・今月の予算）

tkinter のみ（Python標準同梱・追加依存なし）。Windows は透明背景で丸く浮く。
起動: python overlay.py
"""

from __future__ import annotations

import platform
import threading
import tkinter as tk
from tkinter import ttk

import settings

IS_WINDOWS = platform.system() == "Windows"

CHROMA = "#10121a"       # 透明にする背景色（この色のピクセルが透ける・Windows）
CHROMA_RGB = (16, 18, 26)
SIZE = 200               # ウィンドウの一辺(px)。中心の球＋波紋が広がる余白
CENTER = SIZE // 2
CORE_R = 27              # 中心の球の半径（直径54px・控えめ・固定で動かない）
HALO = 15               # 球のまわりの柔らかい光のにじみ
RIPPLE_RANGE = CENTER - CORE_R - 2   # 波紋が広がりきる距離
RIPPLE_SPEED = 0.5       # 波紋が広がる速さ(px/frame)・ゆっくり上品に
RIPPLE_EVERY = 34        # 何フレームごとに波紋を落とすか（≈1.4秒・静かに）

# 配色テーマ（bright=芯の明るさ / body=球の本体色 / ripple=波紋）。settings.json の orb_theme で選ぶ。
THEMES = {
    "moonlight": {"bright": (224, 240, 255), "body": (150, 200, 255), "ripple": (150, 200, 255)},
    "amethyst":  {"bright": (233, 220, 255), "body": (172, 142, 236), "ripple": (176, 150, 240)},
    "mist":      {"bright": (240, 245, 250), "body": (192, 206, 226), "ripple": (202, 216, 236)},
    "ember":     {"bright": (255, 233, 200), "body": (240, 182, 122), "ripple": (240, 186, 132)},
}

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


def _hex(c: tuple) -> str:
    return "#%02x%02x%02x" % tuple(c)


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

        self.theme = THEMES.get(settings.get("orb_theme", "moonlight"), THEMES["moonlight"])
        self._ripples: list[float] = []   # 各波紋の現在半径
        self._tick = 0
        self._settings_win = None
        self._animate()

    # ---- アニメーション（中心は静止、周囲が水の波紋のように広がる）----
    def _animate(self) -> None:
        th = self.theme
        self._tick += 1
        if self._tick % RIPPLE_EVERY == 0:           # 静かに、定期的に波紋を落とす
            self._ripples.append(float(CORE_R))

        c = self.canvas
        c.delete("all")

        # 波紋：中心から外へ広がりながら、外縁ほど CHROMA に溶けて消える（細い輪郭線）
        alive: list[float] = []
        for r in self._ripples:
            r += RIPPLE_SPEED
            prog = (r - CORE_R) / RIPPLE_RANGE       # 0(中心)→1(広がりきり)
            if prog >= 1.0:
                continue
            col = _lerp(th["ripple"], CHROMA_RGB, prog)
            w = max(1, int(round(2.4 * (1 - prog))))  # 外へ行くほど細く
            c.create_oval(CENTER - r, CENTER - r, CENTER + r, CENTER + r, outline=col, width=w)
            alive.append(r)
        self._ripples = alive

        # 球のまわりの柔らかい光のにじみ（外→内へ、body色がCHROMAから立ち上がる＝固いフチを消す）
        for i in range(8, 0, -1):
            t = i / 8
            rr = CORE_R + HALO * t
            c.create_oval(CENTER - rr, CENTER - rr, CENTER + rr, CENTER + rr,
                          fill=_lerp(th["body"], CHROMA_RGB, t), outline="")

        # 中心の球：固定。中心ほど明るい同心円で“淡く光る球”に（反射＝オフセットの白点ではない）
        c.create_oval(CENTER - CORE_R, CENTER - CORE_R, CENTER + CORE_R, CENTER + CORE_R,
                      fill=_hex(th["body"]), outline="")
        r2 = CORE_R * 0.62
        c.create_oval(CENTER - r2, CENTER - r2, CENTER + r2, CENTER + r2,
                      fill=_lerp(th["body"], th["bright"], 0.55), outline="")
        r3 = CORE_R * 0.30
        c.create_oval(CENTER - r3, CENTER - r3, CENTER + r3, CENTER + r3,
                      fill=_hex(th["bright"]), outline="")

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

        # orb の配色（保存で実行中の orb に即反映）
        ttk.Label(win, text="orb の色").grid(row=6, column=0, sticky="w", pady=4)
        _THEME_JA = {"moonlight": "月明かり", "amethyst": "紫水晶", "mist": "霞", "ember": "灯火"}
        ja2key = {v: k for k, v in _THEME_JA.items()}
        cur_theme = _THEME_JA.get(cfg.get("orb_theme", "moonlight"), "月明かり")
        theme_var = tk.StringVar(value=cur_theme)
        ttk.OptionMenu(win, theme_var, cur_theme, *_THEME_JA.values()).grid(row=6, column=1, sticky="ew", pady=4)

        # 今月の予算
        budget_txt = _budget_line()
        ttk.Label(win, text=budget_txt, foreground="#888").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(12, 8))

        def _collect() -> dict:
            return {
                "voice_enabled": voice_var.get(),
                "voicevox_speaker": SPEAKERS.get(name_var.get(), "8"),
                "voicevox_pitch": round(pitch_var.get(), 3),
                "voicevox_speed": round(speed_var.get(), 2),
                "orb_theme": ja2key.get(theme_var.get(), "moonlight"),
            }

        def _apply_theme() -> None:
            self.theme = THEMES.get(_collect()["orb_theme"], THEMES["moonlight"])

        def _save() -> None:
            settings.save(_collect())
            _apply_theme()             # 実行中の orb に即反映
            win.destroy()
            self._settings_win = None

        def _test() -> None:
            settings.save(_collect())  # 今の値で試聴
            _apply_theme()             # 色プレビューも即反映
            threading.Thread(
                target=lambda: _safe_speak("はい、マスター。ちゃんと聞こえていますか？"),
                daemon=True,
            ).start()

        btns = ttk.Frame(win)
        btns.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(6, 0))
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
