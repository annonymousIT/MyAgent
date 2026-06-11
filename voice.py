"""音声入力モード（Step2 / ADR-0035）。声だけで MyAgent と対話する。

パイプライン: マイク → VAD(発話区間検出) → Whisper(ローカルSTT・無料) → core.run_turn
              → テキスト返答を表示 ＋ つむぎ(VOICEVOX)で読み上げ。

設計:
- core.run_turn を使う（web.py / agent.py と同じ中核）。耳と口を足しただけ。
- STTはローカル(faster-whisper)＝API課金ゼロ。脳(haiku)のコストは従来通り。
- 自分の発話で誤起動しないよう、読み上げ中はマイクを開かない（録音→処理→発話→録音…と直列）。
- VOICEVOX未起動なら read 側は say にフォールバック（speak.py）。

起動: source .env && source .venv/bin/activate && python voice.py
       （初回は Whisper モデルのDLあり。マイク許可を一度求められる）
終了: 「終了」「バイバイ」と言う or Ctrl+C
環境変数: WHISPER_MODEL(既定 small), VAD_AGGRESSIVENESS(0-3,既定2), SILENCE_MS(既定800)
"""

from __future__ import annotations

import collections
import os
import sys

import numpy as np
import sounddevice as sd
import webrtcvad

import core
import speak as speak_mod

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME = SAMPLE_RATE * FRAME_MS // 1000           # 480 samples/frame
SILENCE_MS = int(os.environ.get("SILENCE_MS", "600"))
SILENCE_FRAMES = SILENCE_MS // FRAME_MS
PAD_FRAMES = 10                                   # 発話開始判定の前後バッファ
MIN_SPEECH_FRAMES = 8                             # これ未満は雑音として無視
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")  # baseは小型で約4倍速・短い命令なら十分

EXIT_WORDS = ("終了", "バイバイ", "ばいばい", "おやすみ")  # おやすみは挨拶もあるが終了も兼ねる運用
RESET_WORDS = ("リセット", "忘れて", "履歴クリア")
# Whisperが無音/雑音に対して吐く定番の幻聴。これらは無視する。
_HALLUCINATIONS = ("ご視聴ありがとうございました", "ありがとうございました", "おわり", "(", "「")

_vad = webrtcvad.Vad(int(os.environ.get("VAD_AGGRESSIVENESS", "2")))


def _record_utterance() -> "np.ndarray | None":
    """発話を1つ録音して int16 波形を返す。VAD で開始/終了を検出。"""
    ring = collections.deque(maxlen=PAD_FRAMES)
    triggered = False
    voiced: "list[np.ndarray]" = []
    silence = 0
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME) as stream:
        while True:
            block, _ = stream.read(FRAME)
            frame = block[:, 0]
            if len(frame) < FRAME:
                continue
            is_speech = _vad.is_speech(frame.tobytes(), SAMPLE_RATE)
            if not triggered:
                ring.append((frame, is_speech))
                if sum(s for _, s in ring) > 0.6 * ring.maxlen:
                    triggered = True
                    voiced.extend(f for f, _ in ring)
                    ring.clear()
            else:
                voiced.append(frame)
                if is_speech:
                    silence = 0
                else:
                    silence += 1
                    if silence > SILENCE_FRAMES:
                        break
    if len(voiced) < MIN_SPEECH_FRAMES:
        return None
    return np.concatenate(voiced)


def _transcribe(model, audio_i16: "np.ndarray") -> str:
    audio = audio_i16.astype(np.float32) / 32768.0
    segs, _ = model.transcribe(audio, language="ja", beam_size=1)
    return "".join(s.text for s in segs).strip()


def _is_noise(text: str) -> bool:
    if len(text) < 2:
        return True
    return any(h in text for h in _HALLUCINATIONS) and len(text) < 20


def main() -> None:
    print("Whisper モデル読込中…（初回はDLあり）", flush=True)
    from faster_whisper import WhisperModel
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    client = core.build_client()
    persona = core.load_persona()
    history: list = []
    print("🎤 音声入力モード。話しかけてください（「終了」で停止 / Ctrl+C でも可）", flush=True)
    speak_mod.speak("音声入力モードになりました。マスター、話しかけてください。", block=True)

    while True:
        try:
            audio = _record_utterance()
            if audio is None:
                continue
            text = _transcribe(model, audio)
            if not text or _is_noise(text):
                continue
            print(f"\nあなた: {text}", flush=True)

            if any(w in text for w in EXIT_WORDS):
                speak_mod.speak("はい、おつかれさまでした。また呼んでくださいね。", block=True)
                print("終了します。", flush=True)
                break
            if any(w == text.strip("。、 ") for w in RESET_WORDS):
                history = []
                speak_mod.speak("会話の記憶をリセットしました。", block=True)
                continue

            result = core.run_turn(client, persona, text, dry_run=False, history=history)
            history = result["history"]
            reply = result["reply"]
            print(f"MyAgent: {reply}", flush=True)
            c = result.get("usage", {})
            if c:
                print(f"  (¥{c.get('yen', 0)} / {c.get('calls', 0)}コール)", flush=True)
            # 開いた一時タブの後片付け（次発話の頭で閉じる・ADR-0021）
            for url_list in (result.get("ephemeral") or [],):
                pass  # voice単体ではタブ台帳を持たないので即時クローズはしない（web.py用）
            speak_mod.speak(reply, block=True)  # 読み上げ中はマイクを開かない＝自分の声で誤起動しない
        except KeyboardInterrupt:
            print("\n終了します。", flush=True)
            break
        except Exception as e:
            print(f"⚠ {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            continue


if __name__ == "__main__":
    main()
