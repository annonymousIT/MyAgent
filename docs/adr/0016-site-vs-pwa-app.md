# ADR-0016 サイトとPWAアプリの衝突解決（PWA優先）

- 状態: 採用（2026-06-11）

## 課題
同じ名前が「サイト」としても「Chrome PWAアプリ」としても存在することが多い。
スキャンの結果、ユーザーのMacには `~/Applications/Chrome Apps.localized/` に PWA が 22 個あり、YouTube・Gmail・GitHub・moodle+R・manaba・カレンダー等が config の sites と被っている。
「課題見よ」で `moodle+R.app`（専用ウィンドウ）を起動すべきか、`lms.ritsumei.ac.jp`（Chromeタブ）を開くべきか。

## 選択肢
- A. アプリ優先（PWAがあれば起動）
- B. サイト優先（常にブラウザのタブ）
- C. 毎回聞く

## 判断
**A（PWAアプリ優先）**。インストール済みのPWA/アプリがあれば `launch_app` で専用ウィンドウ起動する。`config.user.json` で項目ごとに「サイトで開く」と上書き可能。

## 理由
- PWAは**独立した専用ウィンドウ**で開き、Chromeのタブにならない＝タブグループにも入らない。
- ユーザーがわざわざPWA化した＝「タブの群れから出して専用窓で使いたい」という意思表示と解釈できる。だからアプリを優先するのが意図に沿う。

## 影響
- スキャン（[ADR-0012](0012-app-classification-llm.md)）は `~/Applications/Chrome Apps.localized/` も対象にする（`/Applications` だけでは PWA を取りこぼす）。
- 名前がサイトとアプリで衝突したら、setup はアプリを主として menu に反映。
- `config.user.json` で個別に site 優先へ上書き可能。
