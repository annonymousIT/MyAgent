# ADR-0037 マルチモニタのウィンドウ配置（Windows）

- 状態: 採用（2026-06-11）。[ADR-0025](0025-window-management.md)（ウィンドウ管理）の Windows 拡張。

## 課題
「左の画面にDiscordお願い」のように、**どの物理モニタに置くか**を指定したい。
ADR-0025 はメインディスプレイ前提で、Mac はマルチモニタを別トラック（Hammerspoon）に逃がしていた。

## 判断（Windows）
- `win_ops._monitors()` が **EnumDisplayMonitors + GetMonitorInfo** で全モニタの作業領域を列挙し、
  **x座標で左→右に並べる**。`left`=最左、`right`=最右、番号、`primary`=主モニタで解決。
- `manage_window(action, app, monitor)` に **monitor** を追加。
  - monitor 空 → 現在の画面（OSの真の最大化 SW_MAXIMIZE が使える）
  - monitor 指定 → そのモニタの作業領域に対し action を適用（左半分/右半分/最大化=画面いっぱい/中央）
- LLM への指示（[core.static_menu]）: **画面/モニタ/スクリーン → monitor / 半分・左右寄せ → action**。
  例「左の画面にDiscord」= `manage_window(maximize, Discord, monitor=left)`。
- 毎ターン `[Monitors]: n` を渡し、**1枚なら monitor 指定を無視**させる。

## 影響
- `win_ops.py`: `_monitors` / `_target_rect` / `manage_window(monitor)` / `monitor_count`。
- `tools.py`: `_manage_window_win(monitor)` 委譲・`monitor_count`・ツール定義に monitor。
- 検証（2026-06-11・2画面実機）: #1(2560幅,主)/#2(1920幅) を列挙。左画面最大化=(0,0,2560,1392)、
  右画面最大化=(2560,370,1920,1032)、右画面の左半分=(2560,370,**960**,1032) を実測確認。

## 関連
[ADR-0025] ウィンドウ管理（Mac/純正タイル）。Mac のマルチモニタは別途（monitor 引数は Mac では無視）。
