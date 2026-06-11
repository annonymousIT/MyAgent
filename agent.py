"""音声操作エージェント — ターミナル入力版。

ターミナルで起動すると入力待ちになる。日本語で話しかけると、Claude が意図を判断して
ツール（サイト/アプリ起動・システム操作・ウィンドウ配置・天気・記憶…）を実行し、人格で返す。

頭脳・予算管理・コスト記録はすべて core.run_turn に集約（web.py / voice.py と同じ）。
このファイルは「入力 → run_turn → 表示＋発声」だけの薄い UI。
"""

from __future__ import annotations

import core
import speak as speak_mod

# 返事を音声でも読み上げるか（実行中に「音声オフ」「音声オン」で切替可）
VOICE_ENABLED = True


def main():
    try:
        client = core.build_client()  # 環境変数 or .env から APIキー
    except RuntimeError as e:
        print(f"⚠ {e}")
        return
    persona = core.load_persona()
    voice_on = VOICE_ENABLED
    history: list = []

    # 起動時に今月の予算状況を一言出す（使いすぎに気づける）
    st = core.budget_status()
    print(f"🤖 起動しました（今月 ¥{st['month']}/{st['budget']}・残 ¥{st['remaining']}）。"
          "話しかけてください（終了 exit、音声切替 音声オフ/音声オン）。")
    if st["state"] == "blocked":
        print("⚠ 今月の予算上限に達しています。来月まで API 呼び出しは止まります。")

    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n🤖 おやすみなさいませ。")
            break
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "おやすみ", "ばいばい"):
            print("🤖 おやすみなさいませ。ゆっくり休んでくださいね^^;")
            break
        if user_input in ("音声オフ", "音声オン"):
            voice_on = user_input == "音声オン"
            print(f"🤖 音声を{'オン' if voice_on else 'オフ'}にしました。")
            continue

        result = core.run_turn(client, persona, user_input, history=history)
        history = result.get("history", history)

        for a in result.get("actions", []):  # どのツールが動いたか（実行ログ）
            print(f"   ⚙ {a['result']}")
        print(f"🤖 {result['reply']}")

        c = result.get("cost")  # 実測コスト＋今月の予算状況
        if c:
            mark = {"warn": "⚠", "blocked": "⛔"}.get(c.get("state"), "")
            print(f"   💴 ¥{c['turn']}｜今月 ¥{c['month']}/{c['budget']}（残 ¥{c['remaining']}）{mark}")

        if voice_on and result.get("reply"):
            speak_mod.speak(result["reply"])


if __name__ == "__main__":
    main()
