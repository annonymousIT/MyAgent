# ADR-0041 ウィンドウ状態管理 — 最小化/復元と「しまう vs 終了」の区別

- 状態: 採用（2026-06-12）。[ADR-0037](0037-windows-multimonitor.md)（マルチモニタ配置）の拡張。

## 課題
`manage_window` のアクションは left/right/maximize/center/list の5つだけで、**最小化・復元という基本操作が存在しなかった**。結果:
1. 「しまって/どけて」と「閉じて/終了」を区別できない（どちらも close_app＝アプリ終了になり、消したくないのに落としてしまう）。
2. `windows_overview` は最小化窓も可視扱いで一覧に出すが `IsIconic` を見ておらず、**LLM が「最小化中」と判別できない**（開いているように見える）。
3. 「Spotify 出して」のような**ただ復元するだけの操作**が言えない（左/右/最大化しか無い）。
4. 上記により、ボット操作後にユーザーが最小化等で状態を変えても、ボットの認識が実態とずれて見える。

## 判断
- **`minimize` / `restore` アクションを新設**（`win_ops.manage_window`）。座標計算が不要なので配置系より先に分岐し、`ShowWindow(SW_MINIMIZE/SW_RESTORE)`。restore は `SetForegroundWindow` で前面化（＝「出す」）。
- **最小化タグ `▽`**：`windows_overview` で `IsIconic` を見て最小化窓のタイトル頭に `▽` 1文字。凡例（▽=minimized）と意図区別はプロンプトの**静的（キャッシュ）側**に置く。
- **意図の3分岐をプロンプトに明記**：終了/落として → close_app（アプリを殺す）/ しまって・どけて・隠して → minimize（生かしたまま隠す）/ 出して・戻して → restore。曖昧な「閉じて」は原則 close_app、ただし「どかす」文脈なら minimize 寄り。
- **コスト中立を死守**（[ADR-0039] のコスト美学）：増分は全てキャッシュ側プレフィックス（温時0.1xでほぼ無料、4973tok＝圧縮前5099より下）。毎ターン課金される揮発ブロックの増加は**最小化窓1個につき▽1文字のみ**。
- **「命令を送った」≠「効いた」— 効果を確認してから成功報告**（実機テストで判明した信頼性課題）：
  - close は `WM_CLOSE`（非同期PostMessage）を投げて即成功としていたため、保存ダイアログ/PWAが無視すると「閉じました」と嘘になった → **閉じた後に窓が消えたか再走査**し、残れば再送→それでも残れば False（呼び出し側が正直に報告）。
  - 別モニタ配置は MoveWindow を投げて即成功としていたが Chrome/最大化窓が追従しないことがある → **中心座標が対象モニタに載ったか確認**し、外れていたら一度やり直し、駄目なら正直に失敗。

## 影響
- `win_ops.py`: `SW_MINIMIZE` 定数、`manage_window` の minimize/restore 分岐、`windows_overview` の `IsIconic` タグ。
- `tools.py`: `manage_window` の action enum に minimize/restore 追加・説明更新。
- `core.py`: `static_menu` に quit/minimize/restore の意図区別と ▽ 凡例。
- `selftest.py`: enum 配線＋対象なし時の正直失敗を検証（84 PASS）。
- 残課題: タブ全列挙（ユーザーが手で開いた Chrome タブの把握）は cdp.tabs() のライブ列挙が要るが、揮発を太らせる＝コストと緊張するため**要求時のみ**に留める方針で保留。

## 関連
[ADR-0037] マルチモニタ配置 / [ADR-0040] CDPタブ管理 / [ADR-0039] 月額コスト上限（コスト中立の制約元）。
