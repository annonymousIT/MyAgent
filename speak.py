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
# 既定 6=四国めたん(ツンツン)。ちょい毒舌・世話焼きの女性人格に合わせた選択（ADR-0031）。
# 変えたいときは環境変数 VOICEVOX_SPEAKER で（例: 18=九州そら ツンツン, 9=波音リツ クール）。
VOICEVOX_SPEAKER = os.environ.get("VOICEVOX_SPEAKER", "6")

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


def _voicevox_speak(text: str) -> bool:
    """VOICEVOX で合成して再生。成功したら True。エンジン未起動・失敗なら False（→フォールバック）。"""
    try:
        # 1) テキスト → 音声合成用クエリ（接続拒否ならここで即例外＝待たない）
        q = urllib.parse.urlencode({"text": text, "speaker": VOICEVOX_SPEAKER})
        req = urllib.request.Request(f"{VOICEVOX_URL}/audio_query?{q}", method="POST")
        query = urllib.request.urlopen(req, timeout=2).read()
        # 2) クエリ → WAV
        req2 = urllib.request.Request(
            f"{VOICEVOX_URL}/synthesis?speaker={VOICEVOX_SPEAKER}",
            data=query,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        wav = urllib.request.urlopen(req2, timeout=15).read()
    except Exception:
        return False
    # 3) 再生（一時WAV）
    path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav)
            path = f.name
        if IS_MAC:
            subprocess.run(["afplay", path], check=False)
        elif IS_WINDOWS:
            ps = f"(New-Object Media.SoundPlayer '{path}').PlaySync()"
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)
        else:
            return False
        return True
    except Exception:
        return False
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _os_speak(text: str) -> None:
    """OS標準の音声合成（VOICEVOX が無いときのフォールバック）。"""
    try:
        if IS_MAC:
            # 日本語ボイス指定（英語ボイスだと日本語が記号読みになる）。無ければ素のsayにフォールバック。
            if subprocess.run(["say", "-v", SAY_VOICE, text], check=False).returncode != 0:
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
    if not text:
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
