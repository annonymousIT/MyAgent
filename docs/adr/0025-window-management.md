# ADR-0025 ウィンドウ管理（A+D）— System Events で配置・要アクセシビリティ

- 状態: 採用（2026-06-11）。Issue #23 の実装。[ADR-0019](0019-permission-model.md)（権限＝可逆性）の新能力トラック。

## 課題
「左に寄せて」「最大化して」のようにウィンドウを配置したい。だが macOS で他アプリのウィンドウを
動かすには **アクセシビリティ（Assistive Access）許可が必須**で、許可は手動でしか与えられない。
さらに AppleScript 単体では**マルチモニタの列挙が困難**。

## 判断（v1: AppleScript + メインディスプレイ + 半分/最大/中央）
- 手段は **System Events（AppleScript）**。追加依存ゼロで始める。相性が悪ければ Hammerspoon に逃がす（#23の注記どおり、別トラック）。
- v1 のアクション: **left / right（左右半分）/ maximize / center / list**。`app` 省略時は**最前面**ウィンドウ。
- 配置はメインディスプレイの可視領域（Finder desktop bounds − メニューバー）基準。**マルチモニタ配置は v2（Hammerspoon 候補）**。
- **権限は実行前に奪わない**（ADR-0019）。未許可（-1719 / assistive access）なら**エラーで止めず、許可手順を案内する**。
- アプリ名と System Events の**プロセス名のズレ**（例: 「Chrome」→ 実プロセス "Google Chrome"）は、実在プロセス一覧と突き合わせて吸収（完全一致→NFC→大小無視→部分一致）。

## 影響
- `tools.py`: `manage_window(action, app)` ＋ ヘルパ（`_osa` / `_visible_frame` / `_ui_processes` / `_window_process` / `_ax_denied`）。
- 検証（2026-06-11）: 画面寸法取得・アプリ一覧・プロセス名解決（Chrome→Google Chrome）・**未許可時の案内フォールバック**を実機確認。AppleScript は構文OK（-1719=権限のみ）。
  **実配置は要・手動許可**: システム設定 → プライバシーとセキュリティ → アクセシビリティ で端末アプリを許可後に有効化。

## 関連
[ADR-0019] 権限モデル / [#23] ウィンドウ管理 / v2: Hammerspoon でマルチモニタ。
