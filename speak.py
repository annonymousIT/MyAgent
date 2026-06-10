"""発声（“口”）の最小実装。OS標準の音声合成で喋る。

- Mac:     `say` コマンド
- Windows: PowerShell 経由で System.Speech（SAPI）

将来 VOICEVOX に差し替える際も、この speak(text) のインターフェースを保てば agent.py は無変更で済む。
"""

import platform
import subprocess

IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"


def speak(text: str) -> None:
    """渡した文字列を音声で読み上げる。失敗しても本体を止めない。"""
    if not text:
        return
    try:
        if IS_MAC:
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
