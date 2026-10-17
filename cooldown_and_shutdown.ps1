# Cooldown and Graceful Shutdown Automation
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Step 1: Gracefully Closing Heavy External Applications" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$targetApps = @('chrome', 'msedge', 'discord', 'spotify', 'vlc', 'wmplayer', 'Media.Player')

foreach ($app in $targetApps) {
    $procs = Get-Process -Name $app -ErrorAction SilentlyContinue
    if ($procs) {
        Write-Host "Closing $app ($($procs.Count) process instances)..." -ForegroundColor Yellow
        $procs | ForEach-Object { $_.CloseMainWindow() | Out-Null }
    }
}

Start-Sleep -Seconds 4

foreach ($app in $targetApps) {
    $remaining = Get-Process -Name $app -ErrorAction SilentlyContinue
    if ($remaining) {
        Write-Host "Forcefully terminating lingering $app processes..." -ForegroundColor Gray
        $remaining | Stop-Process -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "External heavy applications closed. IDE and Antigravity session preserved." -ForegroundColor Green
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Step 2: 10-Minute Idle Thermal Cool-Down Countdown" -ForegroundColor Cyan
Write-Host " Allowing fans and heatsink to return to baseline temperatures." -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$totalMinutes = 10
for ($m = $totalMinutes; $m -gt 0; $m--) {
    $timestamp = Get-Date -Format "HH:mm:ss"
    Write-Host "[$timestamp] Cool-down in progress: $m minute(s) remaining..." -ForegroundColor Cyan
    Start-Sleep -Seconds 60
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " Step 3: Cool-Down Complete. Initiating System Shutdown" -ForegroundColor Green
Write-Host " Executing: shutdown /s /t 60" -ForegroundColor Green
Write-Host " (To abort at any time during the 60s window, run: shutdown /a)" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Green

shutdown.exe /s /t 60 /c "Antigravity thermal cool-down complete. Clean system shutdown initiated."
