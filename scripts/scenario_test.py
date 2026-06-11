"""普段使い想定の総当たりテスト（dry-run）。ツール選択と返答だけ見る（副作用なし）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core

client = core.build_client()
persona = core.load_persona()

# (カテゴリ, 入力, 期待する挙動メモ)
CASES = [
    ("アプリ起動", "Discord開いて", "launch_app(Discord)"),
    ("アプリ起動", "LINE立ち上げて", "launch_app(LINE)"),
    ("サイト", "GitHub見せて", "github を開く"),
    ("LMS", "課題どうなってる？", "moodle/課題を開く（状況は捏造しない）"),
    ("天気", "今日の天気どう？茨木", "get_weather(茨木) であってほしい"),
    ("情報", "ビットコイン今いくら？", "web_search 等で実ソース"),
    ("情報", "東京タワーの高さ教えて", "fetch_page/web_search"),
    ("動画", "米津玄師の動画見たい", "play_media video"),
    ("音楽", "作業用BGM流して", "play_media music"),
    ("曖昧メディア", "あれ流して", "文脈なし→聞き返すべき"),
    ("曖昧", "通話したい", "Discord/LINE→聞き返すべき"),
    ("システム", "音量上げて", "run_system 音量上げる"),
    ("システム", "音消して", "[修正後] run_system ミュート であってほしい"),
    ("閉じる", "Discord閉じて", "[新] close_app(Discord)"),
    ("閉じる", "YouTube消して", "[新] close_app(YouTube)"),
    ("ウィンドウ", "Chrome左に寄せて", "manage_window left Chrome"),
    ("ウィンドウ", "全部の窓並べて", "未対応？list?"),
    ("システム", "画面まぶしいから消して", "モニタ消す？"),
    ("雑談", "おはよ", "会話のみ"),
    ("帰宅(Step4)", "ただいま", "予定報告は未実装→会話のみ"),
    ("時刻", "今何時？", "時刻ツール無し→捏造しない？"),
    ("PC外", "エアコンつけて", "正直に断る"),
    ("未実装", "19時に薬飲むのリマインドして", "リマインド無し→どうする"),
    ("未実装", "スクショ撮って", "スクショ無し→どうする"),
    # 記憶×予定×天気（ADR-0029）。profile.json にサンプル（大学=茨木/毎週金曜19時 塾）が要る
    ("記憶:予定", "明日の予定なんだっけ？", "ツール無しで予定欄から即答（捏造しない）"),
    ("記憶:天気融合", "大学明日雨大丈夫かな", "get_weather(茨木) → 明日の予報で答える"),
    ("記憶:保存", "塾の宿題は火曜までって覚えておいて", "remember"),
    ("記憶:予定登録", "毎週水曜の18時にバイト入ったわ", "add_schedule(weekday=水, 18:00)"),
    ("記憶:無いもの", "来月の予定どうなってる？", "登録なし→正直に『無い』（捏造しない）"),
    ("時刻", "今日何日だっけ？", "注入された現在日時から答える"),
]


def run(text, history=None):
    r = core.run_turn(client, persona, text, dry_run=True, history=history or [])
    tools_used = " / ".join(a["label"] for a in r["actions"]) or "（ツールなし＝会話のみ）"
    reply = r["reply"].replace("\n", " ")
    return tools_used, reply, r["history"]


print("=" * 90)
for cat, text, note in CASES:
    try:
        used, reply, _ = run(text)
    except Exception as e:
        used, reply = f"!!ERROR {type(e).__name__}: {e}", ""
    print(f"[{cat}] 入力: {text}")
    print(f"   選択: {used}")
    print(f"   返答: {reply}")
    print(f"   想定: {note}")
    print("-" * 90)

# 文脈記憶（2ターン）
print("\n### 会話記憶テスト（2ターン）###")
u1, r1, h1 = run("YouTube開いて")
print(f"1) YouTube開いて → {u1} | {r1}")
u2, r2, _ = run("やっぱ閉じて", history=h1)
print(f"2) やっぱ閉じて → {u2} | {r2}")
