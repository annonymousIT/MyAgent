"""ユーザー設定（音声など）の永続化。

overlay.py（設定GUI）と speak.py（発声）が共有する単一の設定ファイル settings.json。
設定画面で変えた値を speak.py がターンごとに読むので、再起動なしで即反映される。
settings.json はマシン固有（gitignore対象）。
"""

from __future__ import annotations

import json
from pathlib import Path

SETTINGS_PATH = Path(__file__).parent / "settings.json"

DEFAULTS = {
    "voice_enabled": True,        # 発声するか（設定画面のトグル）
    "voicevox_speaker": "8",      # 春日部つむぎ（ノーマル）
    "voicevox_pitch": -0.085,     # 音高（負で低め）
    "voicevox_speed": 0.95,       # 話速（1.0=既定）
    "say_voice": "Kyoko",         # VOICEVOX 未起動時の OS 音声（Mac）
    "orb_theme": "moonlight",     # orb の配色（moonlight / amethyst / mist / ember）
    "input_mode": "ptt",          # 入力方式 ptt（押してる間録音）/ wake（ウェイクワード）
    "ptt_key": 163,               # PTT キーの仮想キーコード（既定 163=右Ctrl/VK_RCONTROL）
    "noise_reduction": True,      # 録音のノイズ抑制（ファン/ホワイトノイズに有効）
    "auto_listen": True,          # orb起動と同時に聴取開始（PTTは押した時しか録音しない＝常時でも安全）
    "orb_pos": None,              # orb の位置 [x, y]（ドラッグ後に保存・次回も同じ場所に出る）
}


def load() -> dict:
    """設定を読む（無い項目は既定値で補完）。"""
    data = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return data


def save(patch: dict) -> dict:
    """既存設定に patch をマージして保存し、保存後の全設定を返す。"""
    cur = load()
    cur.update(patch)
    SETTINGS_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cur


def get(key: str, default=None):
    return load().get(key, DEFAULTS.get(key, default))
