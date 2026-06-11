# ADR-0034 Googleカレンダー読み取り（iCal秘密URL・読み取り専用）

- 状態: 採用（2026-06-11）。ユーザー選択（iCal秘密URL方式・音声入力の前にやる）。

## 課題
予定が MyAgent ローカル(profile.json)だけに保存され、実生活の予定（大学の授業等＝Googleカレンダー）と
分断（サイロ化）。「明日の予定」と聞いても手動登録分しか出ない。

## 判断
- **読み取り専用 × OAuth不要**: Googleカレンダー設定の「iCal形式の限定公開URL（secret address）」を
  環境変数 `GOOGLE_ICAL_URL`（.env=gitignore済）に置き、HTTP GET でiCalを取得。
- 書き込み（API登録）は重い(OAuth要)ので当面しない。add_schedule は引き続きローカル保存。
- `icalendar` + `recurring_ical_events` で繰り返し予定（毎週の授業=RRULE）を実日付に展開。
- **既存設計に吸収**: calendar_src.upcoming_events() は profile_store.upcoming() と同形式 (date, time, title) を返し、
  context_text の「直近7日」でローカル予定とマージ。→ **新ツール不要**で「明日の予定？」も読み上げも両ソース統合。
- **落ちない/安い**: URL未設定・取得失敗・パース失敗は空（or 直近キャッシュ）を返す。毎ターン取得せず TTL 30分
  （GOOGLE_ICAL_TTL）でメモリキャッシュ。注入は7日分のみなのでトークン/コスト増は僅か。

## セキュリティ
iCal秘密URLは「知っている人は予定を読める」資格情報。**.env に置き、絶対にコミットしない**
（チャット貼付も漏洩扱い→その場合 Google 側で再生成）。

## 影響
- 新規 calendar_src.py。profile_store.context_text がローカル＋カレンダーをマージ・重複排除して表示。
- requirements: icalendar, recurring_ical_events 追加。
- 検証: URL未設定でも従来通りローカルのみで動作（壊れない）を確認。実カレンダー結合はURL設定後にE2E。

## 関連
[ADR-0029] 記憶・予定（ローカル） / [#9] カレンダーは全取得→本人分選別（将来の絞り込み余地） / PlanPal連携は別途保留。
