"""音声操作エージェント Step1（テキスト入力版）。

ターミナルで起動すると入力待ちになる。日本語で話しかけると、Claude が意図を判断して
ツール（サイト/アプリ起動・システム操作）を実行し、敬語の世話焼き人格で一言返す。
"""

import os
import sys
from pathlib import Path

import anthropic

import tools
import speak as speak_mod

MODEL = "claude-haiku-4-5"
PERSONA_PATH = Path(__file__).parent / "persona.txt"

# 返事を音声でも読み上げるか（実行中に「音声オフ」「音声オン」で切替可）
VOICE_ENABLED = True


def load_persona() -> str:
    return PERSONA_PATH.read_text(encoding="utf-8").strip()


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("環境変数 ANTHROPIC_API_KEY が未設定です。先にAPIキーをセットしてください。")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    persona = load_persona()
    voice_on = VOICE_ENABLED

    print("🤖 起動しました。話しかけてください（終了は exit / quit、音声切替は 音声オフ / 音声オン）。")
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

        reply = handle(client, persona, user_input)
        print(f"🤖 {reply}")
        if voice_on:
            speak_mod.speak(reply)


def handle(client, persona: str, user_input: str) -> str:
    """1ターン処理：LLMにツール選択させ、実行し、結果を踏まえた返事を返す。"""
    messages = [{"role": "user", "content": user_input}]

    # 1回目：ツールを選ばせる
    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=persona,
        tools=tools.TOOL_DEFS,
        messages=messages,
    )

    # ツールを使わず返事だけのケース（「おはよ」など）
    if response.stop_reason != "tool_use":
        return _text_of(response)

    # ツール実行 → 結果を返してもう一度返事を生成させる
    messages.append({"role": "assistant", "content": response.content})
    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            result = tools.run_tool(block.name, block.input)
            print(f"   ⚙ {result}")
            tool_results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": result}
            )
    messages.append({"role": "user", "content": tool_results})

    final = client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=persona,
        tools=tools.TOOL_DEFS,
        messages=messages,
    )
    return _text_of(final)


def _text_of(response) -> str:
    parts = [b.text for b in response.content if b.type == "text"]
    return "".join(parts).strip() or "（…）"


if __name__ == "__main__":
    main()
