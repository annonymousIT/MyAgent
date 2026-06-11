"""Googleカレンダー（秘密のiCal URL）読み取り（ADR-0034）。

OAuth不要・読み取り専用。Googleカレンダー設定の「iCal形式の限定公開URL」を
環境変数 GOOGLE_ICAL_URL に入れるだけ。HTTP GET → icalendar でパース →
recurring_ical_events で繰り返し予定（毎週の授業等）を実日付に展開して返す。

設計:
- 落ちない: URL未設定・取得失敗・パース失敗なら空（直近キャッシュがあればそれ）を返す。エージェント本体は止めない。
- 安い/速い: 毎ターン取りに行かず TTL(既定30分)でメモリキャッシュ。トークン増は注入する直近予定のみ。
- 戻り値は profile_store.upcoming() と同じ (date, "HH:MM"|"", title) 形式 → そのままマージできる。
"""

from __future__ import annotations

import datetime
import os
import urllib.request

ICAL_URL = os.environ.get("GOOGLE_ICAL_URL", "").strip()
CACHE_TTL = int(os.environ.get("GOOGLE_ICAL_TTL", "1800"))  # 秒

_cache: "dict" = {"raw": None, "at": None}  # 取得した.icsバイト列と取得時刻（monotonicでなくwallで可）


def _fetch_ics(now: datetime.datetime) -> "bytes | None":
    """.ics を取得（TTL内はキャッシュ）。失敗時は直近キャッシュ or None。"""
    if not ICAL_URL:
        return None
    if _cache["raw"] is not None and _cache["at"] is not None:
        if (now - _cache["at"]).total_seconds() < CACHE_TTL:
            return _cache["raw"]
    try:
        url = "https://" + ICAL_URL.split("://", 1)[1] if "://" in ICAL_URL else ICAL_URL
        req = urllib.request.Request(url, headers={"User-Agent": "MyAgent/1.0"})
        raw = urllib.request.urlopen(req, timeout=6).read()
        _cache["raw"], _cache["at"] = raw, now
        return raw
    except Exception:
        return _cache["raw"]  # 取れなければ前回分（無ければNone）


def upcoming_events(days: int = 7, today: "datetime.date | None" = None):
    """今日から days 日分の予定を [(date, "HH:MM"|"", title), ...] で返す。失敗時は []。"""
    now = datetime.datetime.now()
    today = today or now.date()
    raw = _fetch_ics(now)
    if not raw:
        return []
    try:
        import icalendar
        import recurring_ical_events
        cal = icalendar.Calendar.from_ical(raw)
        end = today + datetime.timedelta(days=days)
        evs = recurring_ical_events.of(cal).between(today, end)
    except Exception:
        return []

    out = []
    for e in evs:
        try:
            title = str(e.get("SUMMARY", "")).strip() or "(無題)"
            dt = e.get("DTSTART").dt
            if isinstance(dt, datetime.datetime):
                if dt.tzinfo is not None:
                    dt = dt.astimezone()  # ローカルタイムへ
                d, tm = dt.date(), dt.strftime("%H:%M")
            else:  # 終日予定（date）
                d, tm = dt, ""
            if today <= d < end:
                out.append((d, tm, title))
        except Exception:
            continue
    out.sort(key=lambda x: (x[0], x[1]))
    return out
