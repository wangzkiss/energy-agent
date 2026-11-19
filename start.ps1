# Start the energy-efficiency Agent (BFF + web) with one command.
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File "E:\Desktop\zuolin\energy-agent\start.ps1"
# If a service is already running on its port, this script leaves it alone and reports it.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$serverDir = Join-Path $root "server"
$webDir = Join-Path $root "web"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Test-Port([int]$Port) {
    try {
        $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        return [bool]$connections
    } catch {
        return $false
    }
}

function Test-BffHealth {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8088/health" -TimeoutSec 2
        return ($health.status -eq "ok")
    } catch {
        return $false
    }
}

function Resolve-Executable([string[]]$Candidates) {
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    return $null
}

$python = Resolve-Executable @(
    "C:\Users\2025\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe",
    (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
)
if (-not $python) {
    throw "Python not found. Set Python in PATH or update the candidate path in start.ps1."
}

$node = Resolve-Executable @(
    "C:\Users\2025\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe",
    (Get-Command node.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
)
$pnpm = Resolve-Executable @(
    "C:\Users\2025\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\pnpm\bin\pnpm.mjs",
    (Get-Command pnpm.cmd -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source),
    (Get-Command pnpm.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
)
if (-not $pnpm) {
    throw "pnpm not found. Set pnpm in PATH or update the candidate path in start.ps1."
}
if (-not $node -and $pnpm -match "\.(mjs|js)$") {
    throw "Node.js not found, but pnpm is a .mjs file."
}

if (-not (Test-Path -LiteralPath (Join-Path $serverDir ".env"))) {
    Write-Warning "server\.env was not found. The BFF may start without a model key or Archery configuration."
}

$webPort = 8090
if ($env:CHAT_WEB_PORT) {
    $webPort = [int]$env:CHAT_WEB_PORT
}

$bffProcess = $null
if (Test-BffHealth) {
    Write-Host "[BFF] already running: http://127.0.0.1:8088" -ForegroundColor Green
} elseif (Test-Port 8088) {
    throw "Port 8088 is already listening, but http://127.0.0.1:8088/health is not healthy. Stop the process on 8088 first."
} else {
    $bffOut = Join-Path $logDir "bff.out.log"
    $bffErr = Join-Path $logDir "bff.err.log"
    $bffProcess = Start-Process -FilePath $python `
        -ArgumentList @("-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8088") `
        -WorkingDirectory $serverDir `
        -RedirectStandardOutput $bffOut `
        -RedirectStandardError $bffErr `
        -WindowStyle Hidden `
        -PassThru
    Write-Host "[BFF] starting (PID $($bffProcess.Id)) ..." -ForegroundColor Cyan
}

$webProcess = $null
if (Test-Port $webPort) {
    Write-Host "[WEB] port $webPort already listening: http://127.0.0.1:$webPort" -ForegroundColor Green
} else {
    $webOut = Join-Path $logDir "web.out.log"
    $webErr = Join-Path $logDir "web.err.log"
    if ($pnpm -match "\.(mjs|js)$") {
        $webProcess = Start-Process -FilePath $node `
            -ArgumentList @($pnpm, "dev") `
            -WorkingDirectory $webDir `
            -RedirectStandardOutput $webOut `
            -RedirectStandardError $webErr `
            -WindowStyle Hidden `
            -PassThru
    } else {
        $webProcess = Start-Process -FilePath $pnpm `
            -ArgumentList @("dev") `
            -WorkingDirectory $webDir `
            -RedirectStandardOutput $webOut `
            -RedirectStandardError $webErr `
            -WindowStyle Hidden `
            -PassThru
    }
    Write-Host "[WEB] starting (PID $($webProcess.Id)) ..." -ForegroundColor Cyan
}

if ($bffProcess) {
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (Test-BffHealth) { break }
        if ($bffProcess.HasExited) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-BffHealth)) {
        Write-Host "[BFF] failed to become healthy. Last error log:" -ForegroundColor Red
        if (Test-Path -LiteralPath (Join-Path $logDir "bff.err.log")) {
            Get-Content -LiteralPath (Join-Path $logDir "bff.err.log") -Tail 30
        }
        exit 1
    }
    Write-Host "[BFF] ready: http://127.0.0.1:8088" -ForegroundColor Green
}

if ($webProcess) {
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $webPort) { break }
        if ($webProcess.HasExited) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-Port $webPort)) {
        Write-Host "[WEB] failed to listen on port $webPort. Last error log:" -ForegroundColor Red
        if (Test-Path -LiteralPath (Join-Path $logDir "web.err.log")) {
            Get-Content -LiteralPath (Join-Path $logDir "web.err.log") -Tail 30
        }
        exit 1
    }
    Write-Host "[WEB] ready: http://127.0.0.1:$webPort" -ForegroundColor Green
}

Write-Host ""
Write-Host "Energy Agent is ready." -ForegroundColor Green
Write-Host "Frontend: http://127.0.0.1:$webPort"
Write-Host "BFF:      http://127.0.0.1:8088"
Write-Host "Logs:     $logDir"
