# -*- coding: utf-8 -*-
"""オフライン総合自己テスト（API/マイク不要・¥0）。ロジックの穴を洗い出す。
実データを汚さないよう各モジュールのファイルパスは temp に差し替える。"""
import datetime
import sys
import tempfile
from pathlib import Path

PASS = 0
FAIL = 0
FAILS = []


def ok(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILS.append(label)
        print(f"  ✗ FAIL: {label}")


def section(name):
    print(f"\n=== {name} ===")


TMP = Path(tempfile.mkdtemp())

# ---------------------------------------------------------------- settings
section("settings 永続化")
import settings
settings.SETTINGS_PATH = TMP / "settings.json"
ok(settings.load()["voicevox_speaker"] == "8", "settings 既定 speaker=8")
settings.save({"voicevox_speaker": "3", "voice_enabled": False, "orb_theme": "ember"})
ok(settings.load()["voicevox_speaker"] == "3", "settings 保存→読込 speaker")
ok(settings.load()["voice_enabled"] is False, "settings voice_enabled 保存")
ok(settings.get("orb_theme") == "ember", "settings get orb_theme")
ok(settings.get("存在しない", "dflt") == "dflt", "settings get 既定値")

# ---------------------------------------------------------------- speak
section("speak テキスト整形・設定連携")
import speak as spk
ok(spk.clean_for_speech("やった😅ね") == "やったね", "絵文字除去")
ok(spk.clean_for_speech("はい^^;") == "はい", "顔文字^^;除去")
ok("、" in spk.clean_for_speech("えっと…そう"), "三点リーダ→読点")
ok(spk.clean_for_speech("**太字** `code`").find("*") == -1, "Markdown除去")
ok(spk.clean_for_speech("   　 ") == "", "空白のみ→空")
sp, pi, spd, vol = spk._voice_cfg()
ok(sp == "3", "_voice_cfg が settings 反映(speaker=3)")
ok(isinstance(vol, float), "_voice_cfg が音量も返す(4要素)")
ok(spk.voice_enabled() is False, "voice_enabled が settings 反映(False)")
settings.save({"voice_enabled": True})  # 戻す

# ---------------------------------------------------------------- wake word
section("ウェイクワード判定")
import voice
cases_hit = {
    "やはーエージェントメモ開いて": "メモ開いて",       # 実Whisper結果
    "やっほーエージェント、Discord開いて": "Discord開いて",
    "ヤッホーエージェント 明日の天気は": "明日の天気は",
    "エージェント、おはよう": "おはよう",
    "やほエージェント音量下げて": "音量下げて",
    "やっほーエージェント": "",                       # 呼びかけのみ
}
for t, exp in cases_hit.items():
    ok(voice._find_wake(t) == exp, f"wake hit: {t!r}→{exp!r}")
cases_none = ["Discord開いて", "ねえちょっとそこ見て", "そのエージェントがさ", "やっほー元気？", ""]
for t in cases_none:
    ok(voice._find_wake(t) is None, f"wake ignore: {t!r}")
ok(voice._norm("　やっ ほー。") == "やっほー", "_norm 全半角/空白/句点処理")

# ---------------------------------------------------------------- tools 解決
section("tools アプリ/サイト解決")
import json
import tools
cfg = tools.load_config()
apps = cfg.get("apps", {})
ok(len(apps) > 50, f"config.auto.json に多数アプリ({len(apps)}件)")
# エイリアス解決（NFC・大小・別名）
ok(tools._resolve_app(apps, "ライン") == tools._resolve_app(apps, "LINE") is not None
   or tools._resolve_entry(apps, "ライン") is not None, "エイリアス『ライン』→LINE")
ok(tools._resolve_entry(apps, "すちーむ") is not None or tools._resolve_entry(apps, "Steam") is not None,
   "Steam 解決")
ok(tools._resolve_entry(apps, "存在しないアプリXYZ") is None, "未登録は None")
# UWP の target は shell:AppsFolder 形式
uwp = [v for v in apps.values() if isinstance(v, dict) and v.get("kind") == "uwp"]
ok(all(v["target"].startswith("shell:AppsFolder") for v in uwp) if uwp else True,
   "UWP target が shell:AppsFolder 形式")
ok(tools.monitor_count() >= 1, "monitor_count>=1")

# ---------------------------------------------------------------- profile_store
section("profile_store 記憶・予定")
import profile_store as ps
ps.PROFILE_PATH = TMP / "profile.json"
ok("覚えました" in ps.remember("彼女はあやさん"), "remember 1件目")
ok("既に覚えて" in ps.remember("彼女はあやさん"), "remember 重複スキップ")
ok("覚える内容が空" in ps.remember("  "), "remember 空")
# 毎週
r = ps.add_schedule("塾", weekday="金", time="19:00")
ok("毎週金曜" in r, "add 毎週")
ok("既に登録済み" in ps.add_schedule("塾", weekday="金", time="19:00"), "add 毎週 重複")
ok("解釈できません" in ps.add_schedule("x", weekday="ニチ"), "add 不正曜日")
# 来週の曜日（once）= 今日より後の最初の該当日
today = datetime.date.today()
r = ps.add_schedule("ゼミ", weekday="月", once=True)
ok("登録しました" in r, "add 来週の月曜(once)")
# date 指定
ok("登録しました" in ps.add_schedule("GD", date="2026-12-25", time="13:00"), "add 日付指定")
ok("解釈できません" in ps.add_schedule("x", date="2026/12/25"), "add 不正日付")
ok("内容（タイトル）が空" in ps.add_schedule(""), "add 空タイトル")
# forget
ok("忘れました" in ps.forget("あやさん"), "forget 事実")
ok("該当する記憶" in ps.forget("存在しないものzzz"), "forget 無し")
ok("忘れました" in ps.forget("塾"), "forget 予定(タイトル)")
# context_text（日付フォーマットがWindowsで落ちないか＝最重要）
ctx = ps.context_text()
ok("[Now]" in ctx and "[Date map" in ctx, "context_text 生成OK(strftime非依存)")
# upcoming
ps.add_schedule("テスト予定", date=(today + datetime.timedelta(days=2)).isoformat())
up = ps.upcoming(7, today)
ok(any(t == "テスト予定" for _, _, t in up), "upcoming に登録予定が出る")

# ---------------------------------------------------------------- core コスト
section("core コストガード")
import core
core.COSTS_PATH = TMP / "costs.jsonl"
core.MONTHLY_BUDGET_YEN = 300.0
ok(core.month_spent() == 0.0, "月初コスト0")
st = core.budget_status()
ok(st["state"] == "ok" and st["remaining"] == 300, "予算status ok")
# 上限ゲート（client=None でAPIを叩かない＝叩いたら例外で落ちる）
import json as _json
core.COSTS_PATH.write_text("\n".join(_json.dumps({"ts": datetime.datetime.now().isoformat(), "yen": 50, "calls": 1}) for _ in range(7)) + "\n", encoding="utf-8")
ok(core.budget_status()["state"] == "blocked", "350円で blocked")
r = core.run_turn(None, "p", "やっほー", history=[])
ok(r.get("blocked") and r["usage"]["yen"] == 0.0 and r["usage"]["calls"] == 0, "上限超過→API呼ばず¥0")
core.COSTS_PATH.write_text("", encoding="utf-8")
# 警告閾値
core.COSTS_PATH.write_text(_json.dumps({"ts": datetime.datetime.now().isoformat(), "yen": 250, "calls": 1}) + "\n", encoding="utf-8")
ok(core.budget_status()["state"] == "warn", "250円で warn(80%超)")
# _log_cost が今日/今月集計を返す
core.COSTS_PATH.write_text("", encoding="utf-8")
c = core._log_cost({"yen": 1.5, "calls": 2})
ok(c["turn"] == 1.5 and c["month"] >= 1.5, "_log_cost 集計")

# ---------------------------------------------------------------- setup パーサ
section("setup LLM出力パーサの頑健性")
import setup
ok(setup._extract_json('```json\n{"results":[]}\n```') == {"results": []}, "コードフェンス除去")
ok(setup._extract_json('ゴミ {"results":[{"name":"A"}]} 末尾') == {"results": [{"name": "A"}]}, "前後雑文から抽出")
ok(setup._extract_json("壊れた{{{") is None, "壊れJSON→None")
parsed = setup._parse_results('{"results":[{"name":"YouTube","category":"動画","aliases":["ようつべ","ようつべ"," "]}]}')
ok(parsed["YouTube"]["category"] == "動画", "_parse_results category")
ok(parsed["YouTube"]["aliases"] == ["ようつべ"], "_parse_results aliases 重複/空除去")
bad = setup._parse_results('{"results":[{"name":"X","category":"知らない区分","aliases":"配列じゃない"}]}')
ok(bad["X"]["category"] == "その他" and bad["X"]["aliases"] == [], "_parse_results 不正値の正規化")

# ---------------------------------------------------------------- win_ops
section("win_ops Windows操作")
import win_ops
mons = win_ops._monitors()
ok(len(mons) >= 1, f"モニタ列挙({len(mons)}枚)")
ok(all(len(m) == 5 for m in mons), "モニタタプル形状(x,y,w,h,primary)")
run = win_ops.running_apps(tools.load_config())
ok(isinstance(run, list), "running_apps がリスト返す")
ok(win_ops.app_is_running("explorer.exe"), "app_is_running(explorer)=True")
ok(not win_ops.app_is_running("絶対にない.exe"), "app_is_running(存在しない)=False")

# ---------------------------------------------------------------- 新機能（⑦窓把握 / ⑥全画面 / ④CDP / 状態連携）
section("窓把握・全画面・CDP・状態")
ws = tools.windows_summary()
ok(isinstance(ws, str), "windows_summary が文字列")
ok(ws == "" or "mon" in ws, "windows_summary 形式（mon◯:）")
ok(isinstance(win_ops.foreground_is_fullscreen(), bool), "foreground_is_fullscreen が bool")
import cdp
ok(isinstance(cdp.available(), bool), "cdp.available（未起動でも安全に bool）")
ok(cdp.close_by_url([]) == 0, "cdp.close_by_url 空リスト＝0")
ok(cdp.tabs() == [] or cdp.available(), "cdp.tabs CDP無し＝空")
ok(cdp.chrome_path() is None or cdp.chrome_path().lower().endswith("chrome.exe"), "chrome_path 形式")
import voice as _v2
_v2._set_state("thinking")
ok(_v2._STATE_PATH.read_text(encoding="utf-8").strip() == "thinking", "orb_state.txt 書き込み")
_v2._set_state("idle")

# ---------------------------------------------------------------- タイマー/クリップボード/危険操作確認
section("タイマー・クリップボード・危険操作")
ok("セットしました" in tools.set_timer(1, "テスト"), "set_timer 正常")
ok("解釈できません" in tools.set_timer("abc"), "set_timer 不正値")
ok("24時間以内" in tools.set_timer(99999), "set_timer 上限")
ok("キャンセルしました" in tools.cancel_timers(), "cancel_timers")
ok("セット中のタイマーはありません" in tools.cancel_timers(), "cancel_timers 空")
import tkinter as _tk
_r = _tk.Tk(); _r.withdraw(); _r.clipboard_clear(); _r.clipboard_append("テスト用の文章です"); _r.update()
cb = tools.read_clipboard()
_r.destroy()
ok("テスト用の文章です" in cb, "read_clipboard 実テキスト取得")
# 危険操作：confirmed 無し→実行せず確認要求 / input() でブロックしない（音声フリーズバグの回帰防止）
r = tools.run_system("再起動")
ok("未実行" in r and "confirmed=true" in r, "危険操作は confirmed 無しで実行されない")
ok("実行しました" in tools.run_system("ミュート") and "実行しました" in tools.run_system("ミュート解除"),
   "安全操作は即実行（ミュート→解除）")
# barge-in: 中断可能再生が stop_check で早期に止まる
import io as _io, struct as _st, time as _time, wave as _wave
buf = _io.BytesIO()
with _wave.open(buf, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b"\x00\x00" * 16000 * 2)  # 2秒の無音
import speak as _spk
t0 = _time.time()
intr = _spk.play_wav_interruptible(buf.getvalue(), stop_check=lambda: _time.time() - t0 > 0.3)
dt = _time.time() - t0
ok(intr and dt < 1.2, f"barge-in: 2秒のWAVを{dt:.2f}sで中断")

# ---------------------------------------------------------------- 起動スモーク（importミス等で main が落ちるのを検出）
section("voice.py 起動スモーク")
import os as _os
import subprocess as _sp
import sys as _sys
_env = dict(_os.environ, MYAGENT_SMOKE="1", PYTHONUTF8="1")
_r = _sp.run([_sys.executable, "voice.py"], env=_env, capture_output=True, text=True,
             timeout=180, cwd=str(Path(__file__).parent), encoding="utf-8", errors="ignore")
ok("SMOKE_OK" in (_r.stdout or ""), f"voice.py 起動→SMOKE_OK（exit={_r.returncode}）")

# ---------------------------------------------------------------- 結果
print(f"\n{'='*40}\n結果: PASS {PASS} / FAIL {FAIL}")
if FAILS:
    print("失敗:", *FAILS, sep="\n  - ")
    sys.exit(1)
print("全部PASS ✓")
