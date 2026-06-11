"""音声入力モード（Step2 / ADR-0035）。声だけで MyAgent と対話する。

パイプライン: マイク → VAD(発話区間検出) → Whisper(ローカルSTT・無料) → core.run_turn
              → テキスト返答を表示 ＋ つむぎ(VOICEVOX)で読み上げ。

体感速度の工夫（ADR-0035 Update2）:
- フィラー相槌: LLMが0.9秒で返らなければ、起動時に合成済みの短い相槌（「はい」等）を即再生
  → 無音の待ちが消え、応答が速く感じる。
- ウォームアップ: 起動時に Whisper と VOICEVOX を空回しして初回遅延をなくす。
- 音声モードでは天気タブを開かない（読み上げが配信手段。tools.SHOW_WEATHER_PAGE=False）。
- 前ターンで開いた一時タブ（web_search等）は次の発話の頭で自動クローズ→元のアプリへ画面を戻す。
- 返答は短く（personaにVoice mode指示を追加注入）→ TTSも速い。

起動方式（ADR-0038・仮）: ウェイクワード「やっほーエージェント」を含む発話だけ反応する。
  ゲーム中・通話中・独り言は無視＝誤爆しない。一度起動したら AWAKE_WINDOW 秒は呼びかけ不要。

起動: source .env && source .venv/bin/activate && python voice.py
終了: 「終了」「バイバイ」（起動中にその一言だけ言う）or Ctrl+C
環境変数: WHISPER_MODEL(既定 base), VAD_AGGRESSIVENESS(0-3,既定2), SILENCE_MS(既定600),
          WAKE_PHRASE(既定 やっほーエージェント), AWAKE_WINDOW_SEC(既定 20)
"""

from __future__ import annotations

import collections
import os
import random
import re
import sys
import threading
import time
import unicodedata

import numpy as np
import sounddevice as sd
import webrtcvad

import core
import speak as speak_mod
import tools

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME = SAMPLE_RATE * FRAME_MS // 1000           # 480 samples/frame
SILENCE_MS = int(os.environ.get("SILENCE_MS", "600"))
SILENCE_FRAMES = SILENCE_MS // FRAME_MS
PAD_FRAMES = 10                                   # 発話開始判定の前後バッファ
MIN_SPEECH_FRAMES = 8                             # これ未満は雑音として無視
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")  # baseは約5倍速・短い命令なら十分

# その一言だけ言ったときのみ終了（「ゲーム終了して」等の誤爆防止で完全一致）
EXIT_WORDS = ("終了", "バイバイ", "ばいばい")
RESET_WORDS = ("リセット", "忘れて", "履歴クリア")
# Whisperが無音/雑音に対して吐く定番の幻聴。これらは無視する。
_HALLUCINATIONS = ("ご視聴ありがとうございました", "ありがとうございました", "おわり", "(", "「")

# LLM待ちを埋める相槌（起動時にVOICEVOXで合成してメモリに保持→即再生できる）
FILLER_TEXTS = ("はい。", "ええと。", "ちょっと待ってくださいね。")
FILLER_AFTER = 0.9  # 秒。これより早くLLMが返れば相槌なし

# ウェイクワード（ADR-0038・仮）。これを含む発話だけ反応する＝ゲーム中・通話中の声は無視＝誤爆しない。
# 一度起動したら AWAKE_WINDOW 秒は呼びかけ不要で連続会話できる。
WAKE_PHRASE = os.environ.get("WAKE_PHRASE", "やっほーエージェント")
AWAKE_WINDOW = float(os.environ.get("AWAKE_WINDOW_SEC", "20"))
# Whisperの表記ゆれ（やっほー/ヤッホー/やっほ 等）を緩く拾う。「やっほ*＋エージェント」を起動とみなす。
_WAKE_RE = re.compile(r"(やっほ[ーぉお]?|ヤッホ[ーォ]?|ﾔｯﾎ[ｰ]?)\s*エージェント")


def _norm(s: str) -> str:
    """全角半角・空白を均して比較しやすくする。"""
    return unicodedata.normalize("NFKC", s).replace(" ", "").replace("　", "").strip("。、！!？?　 ")


def _find_wake(text: str) -> "str | None":
    """発話からウェイクワードを探す。見つかれば『それ以降のコマンド文字列』を返す
    （呼びかけのみなら ""）。無ければ None（＝無視する）。"""
    n = _norm(text)
    m = _WAKE_RE.search(n)
    if not m:
        return None
    return n[m.end():].lstrip("、,。.！!？? 　")

# 返答を声で届ける前提の追加指示（personaに連結＝音声セッション専用の安定プレフィックス）
VOICE_HINT = ("\n[Voice mode] The reply is spoken aloud by TTS. Keep it to 1-2 short sentences. "
              "No lists, no markdown, no URLs.")

# 一時タブを閉じたあと「画面を戻す」対象にしないアプリ（ブラウザ自身など）
_NO_RESTORE = {"Google Chrome", "Safari", "app_mode_loader"}

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
    # ウォームアップ（初回呼び出しの遅延をここで吸収）
    list(model.transcribe(np.zeros(8000, dtype=np.float32), language="ja", beam_size=1)[0])

    tools.SHOW_WEATHER_PAGE = False  # 音声モード: 天気は読み上げで届ける（タブを開かない）

    # 相槌を事前合成（VOICEVOX未起動なら空＝相槌なしで動く）
    fillers = [w for w in (speak_mod._vv_synth(t) for t in FILLER_TEXTS) if w]

    client = core.build_client()
    persona = core.load_persona() + VOICE_HINT
    history: list = []
    pending_eph: list = []   # 前ターンで開いた一時タブ（次の発話の頭で閉じる）
    pending_front = ""       # 一時タブを開く前に前面だったアプリ（閉じた後ここへ戻す）
    awake_until = 0.0        # この時刻まではウェイクワード無しで連続会話できる

    print(f"🎤 音声入力モード。「{WAKE_PHRASE}」と呼びかけてから話してください"
          f"（起動後{int(AWAKE_WINDOW)}秒は呼びかけ不要 / 「終了」で停止 / Ctrl+C でも可）", flush=True)
    speak_mod.speak(f"音声モードです。{WAKE_PHRASE}、と呼んでくださいね。", block=True)

    while True:
        try:
            print("🎤 …", end="\r", flush=True)
            audio = _record_utterance()
            if audio is None:
                continue
            text = _transcribe(model, audio)
            if not text or _is_noise(text):
                continue

            # ウェイクワードのゲート（ADR-0038）。起動中ウィンドウ外で、ウェイクワードを含まない
            # 発話（ゲーム中の声・通話・独り言）は完全に無視する＝誤爆しない。
            now = time.time()
            wake_cmd = _find_wake(text)
            if wake_cmd is not None:               # 「やっほーエージェント」検出
                awake_until = now + AWAKE_WINDOW
                if not wake_cmd:                   # 呼びかけのみ → 返事して次の発話を待つ
                    print(f"\nあなた: {text}  〔起動〕", flush=True)
                    speak_mod.speak("はい、マスター？", block=True)
                    continue
                text = wake_cmd                    # ウェイク以降をコマンドとして実行
            elif now < awake_until:                # 起動中ウィンドウ → 呼びかけ不要で連続会話
                text = _norm(text)
            else:                                  # 待機中 → 無視（誤爆防止の核）
                continue
            print(f"\nあなた: {text}", flush=True)

            stripped = text.strip("。、！!？? 　")
            if stripped in EXIT_WORDS:
                speak_mod.speak("はい、おつかれさまでした。また呼んでくださいね。", block=True)
                print("終了します。", flush=True)
                break
            if stripped in RESET_WORDS:
                history = []
                speak_mod.speak("会話の記憶をリセットしました。", block=True)
                continue

            # 前ターンの一時タブ（web_search等）をここで閉じ、元のアプリへ画面を戻す（ADR-0021）
            if pending_eph:
                closed = tools.close_browser_tabs(pending_eph)
                if closed and pending_front and pending_front not in _NO_RESTORE:
                    tools.activate_app(pending_front)
                pending_eph, pending_front = [], ""

            front = tools._front_process() if speak_mod.IS_MAC else ""

            # LLMを別スレッドで回し、0.9秒で返らなければ相槌を即再生（無音の待ちを消す）
            box: dict = {}

            def _work() -> None:
                try:
                    box["r"] = core.run_turn(client, persona, text, dry_run=False, history=history)
                except Exception as e:  # noqa: BLE001
                    box["e"] = e

            th = threading.Thread(target=_work, daemon=True)
            th.start()
            th.join(FILLER_AFTER)
            if th.is_alive() and fillers:
                speak_mod._play_wav(random.choice(fillers))
            th.join()
            if "e" in box:
                raise box["e"]
            result = box["r"]

            history = result["history"]
            reply = result["reply"]
            print(f"MyAgent: {reply}", flush=True)
            u = result.get("usage", {})
            if u:
                print(f"  (¥{u.get('yen', 0)} / {u.get('calls', 0)}コール)", flush=True)
            speak_mod.speak(reply, block=True)  # 読み上げ中はマイクを開かない＝自分の声で誤起動しない
            awake_until = time.time() + AWAKE_WINDOW  # 対話のたびに起動ウィンドウを延長

            eph = result.get("ephemeral") or []
            if eph:  # このターンで開いた一時タブは次の発話の頭で閉じて画面を戻す
                pending_eph, pending_front = eph, front
        except KeyboardInterrupt:
            print("\n終了します。", flush=True)
            break
        except Exception as e:  # noqa: BLE001
            print(f"⚠ {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            continue


if __name__ == "__main__":
    main()
