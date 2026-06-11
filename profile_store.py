"""永続パーソナルプロファイル＋簡易スケジュール（ADR-0029）。

主人の個人情報（事実）と予定を profile.json にローカル永続化し、
毎ターンのシステムプロンプトに「現在日時＋プロフィール＋直近の予定」を注入する材料を作る。

- profile.json は個人情報なので .gitignore 済み（コミットしない）。雛形は profile.example.json。
- 日付・曜日の計算は全部この Python 側で行う（LLM に日付を計算・捏造させない）。
- Google カレンダー連携は将来拡張（#9）。まずはローカル簡易版。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

BASE = Path(__file__).parent
PROFILE_PATH = BASE / "profile.json"

_WD_JA = ["月", "火", "水", "木", "金", "土", "日"]  # datetime.weekday() 順


def _empty() -> dict:
    return {
        "user": {},               # {"name": "...", "call": "..."}
        "facts": [],              # 自由記述の事実（「彼女はあやさん」等）
        "places": {},             # 場所語の解決表 {"大学": "茨木", ...}（天気・移動用）
        "default_location": "",   # 場所未指定時の既定地名
        "schedule": {"weekly": [], "dated": []},
        # weekly: {"weekday": "金", "time": "19:00", "title": "塾"}
        # dated:  {"date": "2026-06-15", "time": "13:00", "title": "ゼミ発表"}
    }


def load() -> dict:
    """profile.json を読み、欠けたキーを既定値で埋めて返す（無ければ空構造）。"""
    p = _empty()
    if PROFILE_PATH.exists():
        try:
            raw = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return p  # 壊れたJSONでも本体を止めない（記憶なし扱い）
        for k in ("user", "places"):
            if isinstance(raw.get(k), dict):
                p[k] = raw[k]
        if isinstance(raw.get("facts"), list):
            p["facts"] = [str(f) for f in raw["facts"]]
        if isinstance(raw.get("default_location"), str):
            p["default_location"] = raw["default_location"]
        sched = raw.get("schedule") or {}
        for k in ("weekly", "dated"):
            if isinstance(sched.get(k), list):
                p["schedule"][k] = [e for e in sched[k] if isinstance(e, dict)]
    return p


def save(p: dict) -> None:
    PROFILE_PATH.write_text(
        json.dumps(p, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def default_location() -> str:
    return load().get("default_location", "")


# --------------------------------------------------------------------------
# 記憶ツールの実体（remember / add_schedule / forget）
# --------------------------------------------------------------------------
def remember(fact: str) -> str:
    """自由記述の事実を1件追記する（「〜を覚えておいて」）。重複はスキップ。"""
    fact = (fact or "").strip()
    if not fact:
        return "覚える内容が空です。"
    p = load()
    if fact in p["facts"]:
        return f"「{fact}」は既に覚えています。"
    p["facts"].append(fact)
    save(p)
    return f"覚えました：「{fact}」（profile.json に永続保存）。"


def _norm_weekday(w: str) -> "int | None":
    """『金』『金曜』『金曜日』→ 4。解釈できなければ None。"""
    w = (w or "").strip().replace("曜日", "").replace("曜", "")
    return _WD_JA.index(w) if w in _WD_JA else None


def add_schedule(title: str, weekday: str = "", date: str = "", time: str = "", once: bool = False) -> str:
    """予定を追加する。

    - weekday + once=False … 毎週の繰り返し
    - weekday + once=True  … 「来週の月曜」「今度の金曜」など1回限り。→ 次に来るその曜日の実日付を
                              Python側で算出して日付指定として登録（日付計算をLLMにさせない＝誤りを防ぐ）
    - date(YYYY-MM-DD)     … 日付指定
    """
    title = (title or "").strip()
    if not title:
        return "予定の内容（タイトル）が空です。"
    time = (time or "").strip()
    p = load()
    if weekday and weekday.strip():
        wd = _norm_weekday(weekday)
        if wd is None:
            return f"曜日『{weekday}』が解釈できません（月〜日で指定してください）。"
        if once:
            # 次に来るその曜日（今日より後の最初の該当日）を Python が算出
            today = datetime.date.today()
            delta = (wd - today.weekday()) % 7 or 7
            d = today + datetime.timedelta(days=delta)
            entry = {"date": d.isoformat(), "time": time, "title": title}
            if entry not in p["schedule"]["dated"]:
                p["schedule"]["dated"].append(entry)
                save(p)
            return f"{d.isoformat()}({_WD_JA[d.weekday()]}) {time or '(時刻未定)'} に「{title}」を登録しました。"
        entry = {"weekday": _WD_JA[wd], "time": time, "title": title}
        if entry in p["schedule"]["weekly"]:
            return f"毎週{_WD_JA[wd]}曜 {time} {title} は既に登録済みです。"
        p["schedule"]["weekly"].append(entry)
        save(p)
        return f"毎週{_WD_JA[wd]}曜 {time or '(時刻未定)'} に「{title}」を登録しました。"
    if date and date.strip():
        try:
            d = datetime.date.fromisoformat(date.strip())
        except ValueError:
            return f"日付『{date}』が解釈できません（YYYY-MM-DD で指定してください）。"
        entry = {"date": d.isoformat(), "time": time, "title": title}
        if entry in p["schedule"]["dated"]:
            return f"{d.isoformat()} {time} {title} は既に登録済みです。"
        p["schedule"]["dated"].append(entry)
        save(p)
        return f"{d.isoformat()}({_WD_JA[d.weekday()]}) {time or '(時刻未定)'} に「{title}」を登録しました。"
    return "weekday（毎週）か date（日付指定 YYYY-MM-DD）のどちらかを指定してください。"


def forget(query: str) -> str:
    """query を含む事実・予定を削除する。何を消したか返す（誤爆が分かるように）。"""
    query = (query or "").strip()
    if not query:
        return "何を忘れるか指定してください。"
    p = load()
    removed = []
    kept = []
    for f in p["facts"]:
        (removed if query in f else kept).append(f)
    p["facts"] = kept
    for kind in ("weekly", "dated"):
        kept_s = []
        for e in p["schedule"][kind]:
            label = f"{e.get('weekday', e.get('date', ''))} {e.get('time', '')} {e.get('title', '')}"
            if query in (e.get("title") or "") or query in label:
                removed.append(f"予定: {label.strip()}")
            else:
                kept_s.append(e)
        p["schedule"][kind] = kept_s
    if not removed:
        return f"「{query}」に該当する記憶・予定はありませんでした。"
    save(p)
    return "忘れました：" + " / ".join(removed)


# --------------------------------------------------------------------------
# 直近予定の展開（日付計算は Python 側＝捏造させない）
# --------------------------------------------------------------------------
def upcoming(days: int = 7, today: "datetime.date | None" = None) -> "list[tuple[datetime.date, str, str]]":
    """今日から days 日分の予定を (日付, 時刻, タイトル) で返す（weekly を実日付に展開）。"""
    today = today or datetime.date.today()
    p = load()
    out = []
    for i in range(days):
        d = today + datetime.timedelta(days=i)
        for e in p["schedule"]["weekly"]:
            if _norm_weekday(e.get("weekday", "")) == d.weekday():
                out.append((d, e.get("time", ""), e.get("title", "")))
        for e in p["schedule"]["dated"]:
            if e.get("date") == d.isoformat():
                out.append((d, e.get("time", ""), e.get("title", "")))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


# --------------------------------------------------------------------------
# システムプロンプト注入文（毎ターン）
# --------------------------------------------------------------------------
def context_text(now: "datetime.datetime | None" = None) -> str:
    """現在日時＋プロフィール＋直近予定を簡潔にまとめた注入文。

    ここに書かれたものだけが「記憶している事実」。無いことは捏造しない（personaにも明記）。
    """
    now = now or datetime.datetime.now()
    today = now.date()
    p = load()
    lines = [f"[Now] {today.isoformat()}({_WD_JA[today.weekday()]}) {now.strftime('%H:%M')}"]

    # Date map (this/next/week-after, with weekday). Resolve 来週の月曜 etc. by lookup, never compute.
    def _fmt(d):
        rel = "今日" if d == today else ("明日" if d == today + datetime.timedelta(days=1) else "")
        return f"{rel}{d.strftime('%-m/%-d')}({_WD_JA[d.weekday()]})"
    monday = today - datetime.timedelta(days=today.weekday())
    weeks = []
    for wi, wlabel in enumerate(("今週", "来週", "再来週")):
        days = [monday + datetime.timedelta(days=wi * 7 + j) for j in range(7)]
        days = [d for d in days if d >= today]
        if days:
            weeks.append(f"{wlabel}: " + "、".join(_fmt(d) for d in days))
    lines.append("[Date map — resolve relative dates like 来週の月曜 from this; never compute]\n" + "\n".join(weeks))

    prof = []
    user = p.get("user", {})
    if user.get("name"):
        prof.append(f"name: {user['name']}" + (f" (call: {user['call']})" if user.get("call") else ""))
    if p.get("places"):
        prof.append("place table (use these names for weather/travel): "
                    + "、".join(f"{k}={v}" for k, v in p["places"].items()))
    if p.get("default_location"):
        prof.append(f"default place: {p['default_location']}")
    if p.get("facts"):
        prof.append("known facts: " + " / ".join(p["facts"]))
    if prof:
        lines.append("[About the user — persistent memory, this is all there is]\n- " + "\n- ".join(prof))
    else:
        lines.append("[About the user] nothing remembered yet (use remember to save).")

    ups = upcoming(7, today)
    if ups:
        rows = []
        for d, t, title in ups:
            rel = "今日" if d == today else ("明日" if d == today + datetime.timedelta(days=1) else "")
            label = f"{rel}{d.strftime('%m/%d')}({_WD_JA[d.weekday()]})"
            rows.append(f"{label} {t or '時刻未定'} {title}")
        lines.append("[Next 7 days — all saved events]\n- " + "\n- ".join(rows))
    else:
        lines.append("[Next 7 days] none saved (if asked about plans, honestly say none).")
    lines.append("Never invent anything not in the memory/schedule above; if unknown, say 記憶にない or ask. "
                 "Judge 明日/今日 from [Now]; never compute dates yourself.")
    return "\n".join(lines)
