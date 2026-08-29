$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

function Find-Python {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return $python.Source
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        return $py.Source
    }

    throw 'Python was not found. Please install Python 3.10+ and add python or py to PATH.'
}

function Find-Har {
    $localHar = Join-Path -Path $Root -ChildPath 'chat.z.ai.har'
    if (Test-Path -LiteralPath $localHar) {
        return $localHar
    }

    $parent = Split-Path -Parent $Root
    $parentHar = Join-Path -Path $parent -ChildPath 'chat.z.ai.har'
    if (Test-Path -LiteralPath $parentHar) {
        return $parentHar
    }

    return $null
}

function Ensure-Playwright {
    param(
        [string] $PythonExe
    )

    & $PythonExe -c 'import playwright' 2>$null
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-Host '[glm2api] playwright was not found; installing requirements...' -ForegroundColor Yellow
    & $PythonExe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to install playwright. Try manually: python -m pip install -r requirements.txt'
    }
}

function Test-ExistingGlm2Api {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8008/healthz' -UseBasicParsing -TimeoutSec 1
        $status = $response.Content | ConvertFrom-Json
        return [bool]$status.ok
    } catch {
        return $false
    }
}

function Get-PortListener {
    try {
        return Get-NetTCPConnection -LocalPort 8008 -State Listen -ErrorAction Stop |
            Select-Object -First 1
    } catch {
        return $null
    }
}

if (Test-ExistingGlm2Api) {
    Write-Host '[glm2api] an existing local service is already listening; opening the web UI.' -ForegroundColor Green
    Start-Process 'http://127.0.0.1:8008/'
    return
}

$PortListener = Get-PortListener
if ($PortListener) {
    throw ('Port 8008 is already in use by PID ' + $PortListener.OwningProcess + '. Stop that program or choose another port.')
}

$PythonExe = Find-Python
$HarPath = Find-Har
Ensure-Playwright -PythonExe $PythonExe

Write-Host ''
Write-Host '[glm2api] starting local web service...' -ForegroundColor Cyan
if ($HarPath) {
    Write-Host ('[glm2api] preload HAR: ' + $HarPath)
} else {
    Write-Host '[glm2api] no HAR found; starting without account. Use Browser Login in the web panel.' -ForegroundColor Yellow
}
Write-Host '[glm2api] Web: http://127.0.0.1:8008/'
Write-Host '[glm2api] Login: use Browser Login or upload HAR in the right panel.'
Write-Host ''

$RunArgs = @('.\glm2api.py', '--serve', '--port', '8008', '--fresh-captcha-browser', '--open-web')
if ($HarPath) {
    $RunArgs += @('--har', $HarPath)
}

& $PythonExe @RunArgs
