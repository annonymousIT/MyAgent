# MyAgent: 新しいPC（家族・配布先）向けワンショットセットアップ（Windows）
# やること: ①Python確認 ②venv作成＋依存インストール ③APIキー入力→.env保存
#           ④アプリスキャン(setup.py) ⑤ショートカット作成（デスクトップ＋スタートアップ）
# 使い方:  リポジトリを取得したフォルダで
#          powershell -ExecutionPolicy Bypass -File scripts\setup_new_pc.ps1

$ErrorActionPreference = "Stop"
$base = Split-Path -Parent $PSScriptRoot
Set-Location $base
Write-Host "=== MyAgent セットアップ ===" -ForegroundColor Cyan

# ① Python
$py = $null
foreach ($cand in @("python", "py")) {
    try { $v = & $cand --version 2>$null; if ($v -match "Python 3") { $py = $cand; break } } catch {}
}
if (-not $py) {
    Write-Host "Python が見つかりません。https://www.python.org/downloads/ から 3.12 を入れて再実行してください。" -ForegroundColor Yellow
    exit 1
}
Write-Host "Python: $(& $py --version)"

# ② venv + 依存
if (-not (Test-Path ".venv")) {
    Write-Host "仮想環境を作成中…"
    & $py -m venv .venv
}
Write-Host "依存パッケージをインストール中…（数分かかります）"
& ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
Write-Host "依存OK"

# ③ APIキー
if (-not (Test-Path ".env")) {
    Write-Host ""
    Write-Host "Anthropic APIキーを入力してください（https://console.anthropic.com で発行）"
    $key = Read-Host "ANTHROPIC_API_KEY"
    if ($key) {
        "ANTHROPIC_API_KEY=$key" | Out-File -Encoding ascii .env
        Write-Host ".env に保存しました（gitには入りません）"
    } else {
        Write-Host "スキップ（後で .env に ANTHROPIC_API_KEY=... を書いてください）" -ForegroundColor Yellow
    }
} else {
    Write-Host ".env は既にあります（スキップ）"
}

# ④ アプリスキャン（APIキーがあれば呼び名も自動生成）
if ((Test-Path ".env") -and -not (Test-Path "config.auto.json")) {
    Write-Host "インストール済みアプリをスキャン中…（1〜2分・数円だけAPIを使います）"
    & ".venv\Scripts\python.exe" setup.py
}

# ⑤ ショートカット
& powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "install_shortcuts.ps1")

Write-Host ""
Write-Host "=== 完了！ ===" -ForegroundColor Green
Write-Host "デスクトップの『MyAgent』をダブルクリック → 右Ctrlを押しながら話しかけてください。"
Write-Host "（声を良くするには VOICEVOX https://voicevox.hiroshiba.jp/ を入れて起動しておく）"
