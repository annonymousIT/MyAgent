"""発声（“口”）。まず VOICEVOX を試し、無ければ OS標準の音声合成で喋る。

- VOICEVOX:  localhost:50021 のエンジンが起動していれば高品質な日本語音声（ずんだもん等）
- Mac:       `say` コマンド（フォールバック）
- Windows:   PowerShell 経由で System.Speech（SAPI）（フォールバック）

設計（ADR-0024）: VOICEVOX はハード依存にしない。エンジンが起動していれば自動で使い、
未起動なら即 OS標準にフォールバックする（接続拒否は一瞬で返るのでブロックしない）。
呼び出し側は speak(text) だけ見ればよく、agent.py / web.py は実装差を意識しない。

環境変数:
- VOICEVOX_URL     既定 http://127.0.0.1:50021
- VOICEVOX_SPEAKER 既定 3（ずんだもん・ノーマル）
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request

IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"

VOICEVOX_URL = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021").rstrip("/")
# 既定 8=春日部つむぎ(ノーマル)。ユーザー選定(2026-06-11)。env VOICEVOX_SPEAKER で上書き可。
VOICEVOX_SPEAKER = os.environ.get("VOICEVOX_SPEAKER", "8")
# 音高（トーン）。0.0=既定、負で下げる。割と低めに（落ち着いた印象）。env VOICEVOX_PITCH で調整可。
VOICEVOX_PITCH = float(os.environ.get("VOICEVOX_PITCH", "-0.085"))
# 話速。1.0=既定、小さいほど遅い。ほんの少しだけ遅く。env VOICEVOX_SPEED で調整可。
VOICEVOX_SPEED = float(os.environ.get("VOICEVOX_SPEED", "0.95"))

# `say` 用の日本語ボイス（VOICEVOX未導入時のフォールバック）。英語ボイスだと日本語＝記号読みになる。
SAY_VOICE = os.environ.get("SAY_VOICE", "Kyoko")

# 読み上げ前に消す記号類（顔文字・三点リーダ・絵文字・装飾記号）。テキスト表示はそのまま、音声だけ綺麗に。
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⬀-⯿←-⇿✀-➿]"
)
# 顔文字は「^」起点に限定（日本語に ^ は出ないので 。、！？ を巻き込まない）。"^^;" "(^^;)" "^_^" を除去。
_KAOMOJI = re.compile(r"[（(]?\^[\^_＾;；:：)）(（]*")


def clean_for_speech(text: str) -> str:
    """読み上げ用に整える。顔文字(^^;)・三点リーダ(…)・絵文字・Markdown記号を除去/置換する。"""
    t = _EMOJI.sub("", text)
    t = re.sub(r"[…‥]+", "、", t)          # 三点リーダ→読点(間)
    t = re.sub(r"[*#`>|]+", "", t)          # Markdown装飾
    t = _KAOMOJI.sub("", t)                 # ^^; 等の顔文字
    t = re.sub(r"[ \t]+", " ", t).strip()
    return t


def _voice_cfg() -> "tuple[str, float, float]":
    """発声設定（speaker, pitch, speed）を取得。優先順位：環境変数 > settings.json > 既定。

    settings.json は overlay.py の設定画面が書き換える。ここで毎回読むので再起動なしで反映。
    """
    speaker, pitch, speed = VOICEVOX_SPEAKER, VOICEVOX_PITCH, VOICEVOX_SPEED
    try:
        import settings
        s = settings.load()
        speaker = os.environ.get("VOICEVOX_SPEAKER") or str(s.get("voicevox_speaker", speaker))
        pitch = float(os.environ.get("VOICEVOX_PITCH", s.get("voicevox_pitch", pitch)))
        speed = float(os.environ.get("VOICEVOX_SPEED", s.get("voicevox_speed", speed)))
    except Exception:
        pass
    return speaker, pitch, speed


def voice_enabled() -> bool:
    """設定画面の音声トグル。環境変数 MYAGENT_VOICE=0 でも無効化できる。"""
    if os.environ.get("MYAGENT_VOICE") == "0":
        return False
    try:
        import settings
        return bool(settings.load().get("voice_enabled", True))
    except Exception:
        return True


def _vv_synth(text: str, speaker: str, pitch: float, speed: float) -> "bytes | None":
    """1チャンクを VOICEVOX で合成して WAV を返す。失敗（エンジン未起動含む）は None。"""
    try:
        q = urllib.parse.urlencode({"text": text, "speaker": speaker})
        req = urllib.request.Request(f"{VOICEVOX_URL}/audio_query?{q}", method="POST")
        query = json.loads(urllib.request.urlopen(req, timeout=2).read())
        query["pitchScale"] = pitch
        query["speedScale"] = speed
        req2 = urllib.request.Request(
            f"{VOICEVOX_URL}/synthesis?speaker={speaker}",
            data=json.dumps(query).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        return urllib.request.urlopen(req2, timeout=15).read()
    except Exception:
        return None


def _play_wav(wav: bytes) -> None:
    path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav)
            path = f.name
        if IS_MAC:
            subprocess.run(["afplay", path], check=False)
        elif IS_WINDOWS:
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"(New-Object Media.SoundPlayer '{path}').PlaySync()"], check=False)
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def play_wav_interruptible(wav_bytes: bytes, stop_check=None) -> bool:
    """WAV を再生する。stop_check() が True を返したら即停止（barge-in：読み上げ中に話しかけられる）。

    sounddevice で再生しつつ 50ms ごとに stop_check を見る。中断したら True。
    失敗時は従来の _play_wav にフォールバック（中断不可だが音は出る）。
    """
    try:
        import io as _io
        import time as _time
        import wave as _wave

        import numpy as _np
        import sounddevice as _sd
        with _wave.open(_io.BytesIO(wav_bytes), "rb") as w:
            sr = w.getframerate()
            ch = w.getnchannels()
            data = _np.frombuffer(w.readframes(w.getnframes()), dtype=_np.int16)
        if ch > 1:
            data = data.reshape(-1, ch)
        _sd.play(data, sr)
        if stop_check is None:
            _sd.wait()
            return False
        dur = len(data) / sr
        t0 = _time.time()
        while _time.time() - t0 < dur:
            if stop_check():
                _sd.stop()
                return True
            _time.sleep(0.05)
        _sd.wait()
        return False
    except Exception:
        _play_wav(wav_bytes)
        return False


def _voicevox_speak(text: str) -> bool:
    """VOICEVOX で読み上げ。文ごとにストリーミング（最初の一文を合成したら即再生し、
    残りは裏で合成）→ 体感の出だしが速い。成功 True / エンジン未起動など False（→sayへ）。"""
    speaker, pitch, speed = _voice_cfg()
    parts = [p for p in re.split(r"(?<=[。！？!?])", text) if p.strip()]
    if not parts:
        parts = [text]
    first = _vv_synth(parts[0], speaker, pitch, speed)
    if first is None:
        return False  # エンジン未起動 → 呼び出し側で say フォールバック
    import queue as _queue

    q: "_queue.Queue" = _queue.Queue()

    def _produce() -> None:
        for p in parts[1:]:
            q.put(_vv_synth(p, speaker, pitch, speed))
        q.put(None)

    threading.Thread(target=_produce, daemon=True).start()  # 残りを裏で合成
    _play_wav(first)                                        # 最初の一文を即再生
    while True:
        w = q.get()
        if w is None:
            break
        if w:
            _play_wav(w)
    return True


# say(Kyoko)が誤読しやすい語の読み補正（say経路のみ。VOICEVOXは自前辞書が優秀なので適用しない）。
# 文脈で読みを選べないKyokoの応急処置。網羅は無理なので、出やすいものだけ。VOICEVOX導入が本命。
_SAY_READING = {
    "失く": "なく",      # 失くす→×しつく
    "要ら": "いら", "要る": "いる", "要り": "いり",  # 要らない→×かなめら（必要/重要は別表記なので無害）
    "茨木": "いばらき",  # 地名
    "塾": "じゅく",
}


def _say_reading_fixes(text: str) -> str:
    for k, v in _SAY_READING.items():
        text = text.replace(k, v)
    return text


def _say_pauses(text: str) -> str:
    """say は句読点をほぼ素通りするので、文末・読点に無音([[slnc ms]])を挿し込んで間を作る。"""
    text = _say_reading_fixes(text)
    text = re.sub(r"([。！？!?])", r"\1[[slnc 380]]", text)
    text = re.sub(r"([、,])", r"\1[[slnc 130]]", text)
    return text


def _os_speak(text: str) -> None:
    """OS標準の音声合成（VOICEVOX が無いときのフォールバック）。"""
    try:
        if IS_MAC:
            # 日本語ボイス＋文間の無音挿入。英語ボイスだと記号読みになるので -v 指定。無ければ素のsayへ。
            spoken = _say_pauses(text)
            if subprocess.run(["say", "-v", SAY_VOICE, spoken], check=False).returncode != 0:
                subprocess.run(["say", text], check=False)
        elif IS_WINDOWS:
            # シングルクオートをエスケープして PowerShell の SAPI に渡す
            safe = text.replace("'", "''")
            ps = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$s.Speak('{safe}')"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)
    except Exception:
        # 音声が出せない環境でもテキスト返答は成立させる
        pass


def speak(text: str, block: bool = True) -> None:
    """渡した文字列を読み上げる。VOICEVOX→無ければOS標準。失敗しても本体を止めない。

    block=False で別スレッド発声（Web UI が返答表示で待たされないように）。
    """
    if not text or not voice_enabled():  # 設定画面の音声トグル（OFFなら黙る）
        return
    text = clean_for_speech(text)  # 顔文字・三点リーダ・絵文字を除去（音声だけ・表示はそのまま）
    if not text:
        return

    def _run() -> None:
        if not _voicevox_speak(text):
            _os_speak(text)

    if block:
        _run()
    else:
        threading.Thread(target=_run, daemon=True).start()
