# MyAgent: orb のショートカットを作成（Windows）
#  - デスクトップ: 手動起動/再起動用
#  - スタートアップ: ログイン時に orb を自動常駐（聴取は orb をクリックするまで始まらない）
# pythonw.exe で起動するのでコンソール（黒窓）は出ない。
# 使い方:  powershell -ExecutionPolicy Bypass -File scripts\install_shortcuts.ps1

$base = Split-Path -Parent $PSScriptRoot
$pyw = Join-Path $base ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = "pythonw.exe" }  # venv 無ければ素の pythonw

function New-Lnk($path) {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($path)
    $lnk.TargetPath = $pyw
    $lnk.Arguments = "overlay.py"
    $lnk.WorkingDirectory = $base
    $lnk.WindowStyle = 7           # 最小化（万一窓が出ても邪魔しない）
    $lnk.Description = "MyAgent presence orb"
    $lnk.Save()
    Write-Host "作成: $path"
}

$desktop = [Environment]::GetFolderPath('Desktop')
$startup = [Environment]::GetFolderPath('Startup')
New-Lnk (Join-Path $desktop "MyAgent.lnk")
New-Lnk (Join-Path $startup "MyAgent.lnk")
Write-Host "完了。デスクトップの MyAgent をダブルクリックで起動／次回ログインから自動常駐します。"
