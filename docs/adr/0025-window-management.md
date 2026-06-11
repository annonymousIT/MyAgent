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

## Update（2026-06-11）: 純正タイルを第一手段に
自前の座標指定(position/size)はアプリの最小サイズと喧嘩して隙間が出る（実機で右側が空く・左右ピッタリにならない）。
macOS 26 は Window メニューに純正タイル(Move & Resize > Left/Right, Fill)を持つため、**manage_window は純正タイルを第一手段**にし、
座標方式は古いOS・非対応アプリ・center 用のフォールバックに降格。実機で左右が隙間なく画面いっぱいに分割されることを確認
（Discord左 x0/w855・moodle+R右 x855/w855）。Electron系(Discord)は AX のウィンドウ列挙が不安定なため、配置成否の検証は読み戻し値でなく目視を正とする。

## Update2（2026-06-11）: 本体ウィンドウに確実に当てる（かぶり解消）
Discord等Electron系は小さな副ウィンドウを複数持ち、タイルが副窓に当たると本体が並ばず重なる（実機でDiscordが300x300の小窓になり moodle に重なった）。
対策: 純正タイルのクリック前に「面積最大のウィンドウ＝本体」を AXRaise で最前面に上げてから当てる。
全画面スプリット(Full-Screen Tile)は Electron系がメニューに項目を持たず2つ目はピッカー手動選択前提のため自動化を見送り、デスクトップ・タイル(左右半分・隙間なし)を正式採用（ユーザ選択 2026-06-11）。
実機: Discord左 pos(0,39)/size(856,1008)・moodle+R右 pos(856,39)/size(854,1008)で重なりなし・画面いっぱいを確認。
