# ADR-0035 音声入力（Step2）— マイク→VAD→Whisper(ローカル)→core→読み上げ

- 状態: 採用（2026-06-11）。当初ロードマップの最終ピース「声だけで完結」。

## 課題
これまで入力はテキスト（web.py）のみ。本来のビジョン「帰宅後、声だけでPC操作・予定確認が完結」には音声入力が要る。

## 判断
- **新しい"耳口" `voice.py` を core.run_turn の上に作る**（web.py / agent.py と同じ中核を共有。耳と口を足しただけ）。
- パイプライン: sounddevice(マイク) → webrtcvad(発話区間検出) → **faster-whisper(ローカルSTT)** → core.run_turn
  → テキスト返答表示 ＋ speak.py(VOICEVOX=つむぎ/未起動はsay)で読み上げ。
- **STTはローカル＝API課金ゼロ**。脳(haiku)のコストは従来通り＝音声化でコストは増えない（ADR-0032/0033の予算を維持）。
- **エコー対策**: 読み上げ(speak block=True)中はマイクを開かない。録音→STT→core→発話→録音… と直列にして自分の声で誤起動しない。
- **ノイズ/幻聴除去**: 短すぎる発話・Whisperの定番幻聴（「ご視聴ありがとうございました」等）は無視。
- **音声コマンド**: 「終了/バイバイ」で停止、「リセット」で会話記憶クリア。
- L1（ターン制：話す→無音で確定→処理→返答）。ストリーミング/バージイン等のレイテンシ最適化は将来。

## 実装
- voice.py（VAD録音ループ + faster-whisper + core + speak）。env: WHISPER_MODEL(既定small)/VAD_AGGRESSIVENESS/SILENCE_MS。
- requirements: faster-whisper, sounddevice, webrtcvad, numpy 追加。
- 検証(2026-06-11): VOICEVOX生成wav→faster-whisper(small)で「明日の予定を教えて」を一字一句正確に文字起こし
  (モデル読込~20s/発話2.6s)。STT→core連鎖で明日のGoogle＋ローカル統合予定を読む所まで実機確認。
  実マイク録音(VAD)はユーザーが自分のターミナルで `python voice.py` し発話＋マイク許可してE2E（マイク検出済: しょすけ Microphone）。

## 残/将来
- レイテンシ最適化（ストリーミングSTT・バージイン・フィラー埋め）は次段（ADR検討時の議論メモ参照）。
- 能動リマインド（#29）と組むと「声で完結する自分から気遣う相棒」が完成。

## 関連
[ADR-0024] 音声出力 / [ADR-0034] カレンダー / [#28] 音声入力。
