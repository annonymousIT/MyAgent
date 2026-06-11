# MyAgent — 音声操作エージェント

> 話しかけるとPCを操作し、丁寧な敬語の世話焼き人格で返答する自分専用エージェント。
> 帰宅後のPC操作（勉強・ゲーム・予定確認）を、声だけで完結させる。

個人開発者の自分のためのプロダクト。GUIは**あえて持たない**（発話で体験が完結するため）。

---

## コンセプト

- **解く課題**：個人開発者は自分のプロダクトや予定を毎日見にいくのが面倒で離脱しがち。帰宅トリガーで能動的に状況を報告するエージェントがいれば習慣化できる
- **体験の核**：画面を見ずに「発話だけ」で操作が完結する
- **人格**：丁寧な敬語の世話焼き系。表面は礼儀正しいが、ぐうたらをやんわり指摘し、最後は必ず気遣う

---

## アーキテクチャ（4ステップ）

シチュエーションが変わっても、内部処理は常にこの4ステップ。

```
① 耳   音声 → テキスト（Whisper / ローカル無料）   ※Step2以降
② 材料 判断材料を集める（現在時刻・予定・会話履歴・実データ）
③ 脳   材料＋人格を Claude API に渡し、使うツールと返答を判断させる
④ 手＋口 選ばれたツールを実行 ＋ 返答を発声（VOICEVOX）   ※発声はStep3以降
```

仕組みは1つ。違うのは「どの材料が効くか」「どのツールが選ばれるか」だけ。
一度作れば、材料とツールを足すだけでシチュは無限に増える。

---

## ツール設計（“手”は汎用に絞る）

機能ごとにツールを無限に増やさず、**汎用ツール＋設定表**に抽象化する（引き算思想）。

| ツール | 役割 |
|---|---|
| `open_site(name)` | 名前→URL表からURLを引いてブラウザで開く |
| `launch_app(name)` | 名前→アプリ表から引いて起動（Win=exeパス / Mac=`open -a`） |
| `run_system(name)` | 名前→システムコマンド表（音量・モニタ等）を実行。危険操作は確認を挟む |
| `get_weather(location?)` | wttr.in 実データで現在＋3日先の予報。場所未指定はプロファイルの既定地 |
| `remember(fact)` | 個人情報・事実を `profile.json` に永続記憶（「〜覚えておいて」） |
| `add_schedule(title, weekday\|date, time)` | 予定を永続登録（毎週繰り返し / 日付指定） |
| `forget(query)` | 記憶した事実・予定を削除 |
| `read_calendar(date)` | Googleカレンダー（全カレンダー）から予定取得 ※将来（#9） |
| `come_home()` | 「ただいま」で発火。明日の予定＋TODOをまとめて報告 ※Step4 |

拡張は設定表に1行足すだけ。

### 永続パーソナル記憶（ADR-0029）

予定や個人情報（大学の場所・生活リズム等）を `profile.json` にローカル永続化し、
毎ターン「現在日時＋プロフィール＋直近7日の予定」をシステムプロンプトに注入する。
これにより「明日の予定なんだっけ？」「大学（＝茨木）明日雨大丈夫かな」が記憶×実データで成立する。

- `profile.json` は**個人情報なので .gitignore 済み**。`profile.example.json` をコピーして作る。
- 日付・曜日の計算は Python 側（`profile_store.py`）。LLM に日付を計算・捏造させない。

### 設定の2層化（フレームワーク化 step1 / ADR-0011・0012・0016）

身近な人に渡せるよう、アプリ一覧・カテゴリ・日本語の呼び名（略語）を**手で書かず自動生成**する。

- `config.auto.json` … `python setup.py` がOSスキャンで自動生成（再生成可）。
  - PWA：`~/Applications/Chrome Apps.localized/*.app`、native：`/Applications` ほかを走査。
  - Claude（haiku）が各アプリに `category` と日本語 `aliases`（例 YouTube→ようつべ）を付与。
  - 構造：`{"apps": {"<表示名>": {"target","kind":"pwa|native","path","category","aliases":[...]}}}`
- `config.user.json` … 手動層。`sites`（URLは手動必須）/ `system` / `dangerous_system` / 手動追加 `apps` / `overrides`。
- `tools.load_config()` が2層をマージし、**user層がauto層を上書き**する。
  両ファイルが無ければ旧 `config_mac.json` / `config_win.json` にフォールバック（後方互換）。

**PWA優先（ADR-0016）**：同じ名前がサイト(sites)とアプリ(apps)の両方にある時は、専用ウィンドウで開く**アプリ(PWA)を優先**する。

- `launch_app` は表示名だけでなく**エイリアスでも**引ける（menu にエイリアスを併記）。
- `open_site` は、同名のアプリがあれば `launch_app` に委譲する（大小無視で衝突判定）。
- 特定の名前だけサイトで開きたいときは `config.user.json` の `"overrides"` に
  `{"<name>": "site"}` を書く（例：`"github": "site"` で github はブラウザのタブで開く）。

セットアップ：`source .env && source .venv/bin/activate && python setup.py`

### クロスプラットフォーム設計

OS差分は**設定ファイルに隔離**し、コード本体は共通に保つ。

- `config_win.json` … Windows用（exeパス / `nircmd` 等）※2層化前の旧フォールバック
- `config_mac.json` … Mac用（アプリ名 / `osascript`・`pmset` 等）※同上
- `setup.py` のアプリスキャンは現状 **Mac専用**。Windows はスタートメニュー/レジストリ走査でのスキャンが **TODO**。
- 発声 `speak.py` も Mac=`say` / Windows=SAPI を自動切替（口のインターフェースを固定し、将来VOICEVOXに無変更で差し替え可能）

---

## 段階的ロードマップ

```
Step1  テキスト入力 → LLM判断 → サイト/アプリを開く ＋ 敬語で返事   ← いまここ
Step2  音声入力（Whisper, Push to Talk）→ 喋って動く
Step3  音声出力（VOICEVOX）＋ ストリーミングで“間”を詰める
Step4  カレンダー＋TODO読み上げ（「ただいま」帰宅報告）
Step5  記憶の要約管理 ＋ 文脈での動的画面振り分け
```

---

## セットアップ（Step1）

**Windows（PowerShell）:**
```powershell
pip install -r requirements.txt
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python agent.py
```

**Mac（zsh/bash）:**
```bash
pip3 install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
python3 agent.py
```

起動すると返事がテキスト＋音声で返る（`音声オフ` / `音声オン` で切替）。話しかける：

```
> 課題見よ
（manaba をブラウザで開く）
🤖 manaba 開いておきましたよ。ためてないといいんですけど^^;

> ばろやるわ、電話もする
（Valorant 起動 ＋ Discord 起動）
🤖 またですか^^; どちらも開いておきました。あやさん疎かにしないように。
```

### 自分で埋める箇所

- `ANTHROPIC_API_KEY`（環境変数）
- 使うOSの設定ファイル（`config_win.json` または `config_mac.json`）の各URL（manaba 等）とアプリ（Win=実パス / Mac=アプリ名）
- `profile.json`（`profile.example.json` をコピーして名前・場所・予定を書く。会話の「覚えておいて」でも追記される）

---

## 設計上のトレードオフ（設計判断の記録）

| 論点 | 判断 |
|---|---|
| コンテキスト vs コスト | 全履歴を渡さず、直近Nターン＋古い記憶は要約して圧縮 |
| 画面振り分け | 固定ルールは破綻するため、メインの判断をLLMに委ねる |
| 捏造防止 | 数字・予定は必ず実データから取得。知らないことは言わせない |
| GUIレス | 発話で完結するためGUIを意図的に持たない（作らない判断） |
| 自律性の制限 | 起動時自動常駐・遠隔操作は封印。発火は手動、危険操作は確認を挟む |

詳細は [`docs/01_design_memo.md`](docs/01_design_memo.md) を参照。

---

## 技術構成

- **脳**：Claude API（Haiku＝安く十分）／月コスト目安 100〜200円
- **耳**：Whisper（ローカル無料）＋ Focusrite Scarlett マイク ※Step2
- **口**：いまはOS標準音声（Mac=`say` / Win=SAPI）→ Step3でVOICEVOX（世話焼き系の声）に差し替え
- **予定**：Googleカレンダー＋Google ToDo（Tasks API）※Step4
