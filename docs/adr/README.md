# ADR（設計判断の記録）

> ADR = Architecture Decision Record。1判断 = 1ファイル。
> 「課題 → 選択肢 → 判断 → 理由 → 影響」を残し、就活で経緯を即引けるようにする台帳の**正本**。

## 運用フロー
1. 設計変更を**提案**する
2. **二人で承認**する（採用 / 却下 / 保留）
3. 採用したものだけ、このディレクトリに `NNNN-title.md` を追加（番号は Issue 台帳と1:1）
4. `gh` 導入後、`scripts/create_issues.sh` で GitHub Issue にミラー（議論・可視化の窓口）

ADR が正本（git履歴に残る）、Issue は窓口。([ADR-0010](0010-decision-record-method.md) を参照)

## 一覧
| # | タイトル | 状態 |
|---|---|---|
| 0010 | [設計判断の記録方式（ADR正本＋Issue窓口）](0010-decision-record-method.md) | 採用 |
| 0011 | [フレームワーク化：config 2層（auto/user）](0011-framework-two-layer-config.md) | 採用 |
| 0012 | [アプリ分類・呼び名のLLM自動生成](0012-app-classification-llm.md) | 採用 |
| 0013 | [略語・口語の解釈（menu制約＋ファジー＋聞き返し）](0013-abbreviation-handling.md) | 採用 |
| 0014 | [言い間違い・誤認識の吸収（可逆性段階化＋エコー）](0014-misspeak-handling.md) | 採用 |
| 0015 | [ブラウザの開き方（既定・新規タブ・通常プロファイル）](0015-browser-open-behavior.md) | 採用 |
| 0016 | [サイトとPWAアプリの衝突解決（PWA優先）](0016-site-vs-pwa-app.md) | 採用 |
| 0018 | [登録外でもPC内でできる範囲で最大限応える](0018-resourceful-within-pc.md) | 採用 |
| 0019 | [権限・能力モデル（可逆性ベースの3段階）](0019-permission-model.md) | 採用 |
| 0021 | [情報配信モデル（ephemeral/persistent＋自動クローズ）](0021-delivery-model.md) | 採用 |

※ #0001〜0009 は初期の設計判断（[../issues_plan.md](../issues_plan.md) 参照）。本ディレクトリは #0010 以降を正本として管理する。
