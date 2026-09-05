$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root
. (Join-Path -Path $Root -ChildPath 'scripts\startup_helpers.ps1')

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

    $probeExit = Invoke-NativeCommand -FilePath $PythonExe -ArgumentList @('-c', 'import playwright') -Quiet
    if ($probeExit -eq 0) {
        return
    }

    Write-Host '[glm2api] playwright was not found; installing requirements...' -ForegroundColor Yellow
    $installExit = Invoke-NativeCommand -FilePath $PythonExe -ArgumentList @('-m', 'pip', 'install', '-r', 'requirements.txt')
    if ($installExit -ne 0) {
        throw 'Failed to install playwright. Try manually: python -m pip install -r requirements.txt'
    }
}

function Test-HappyDom {
    $node = Get-Command node -ErrorAction SilentlyContinue
    $probeScript = Join-Path -Path $Root -ChildPath 'scripts\check_happydom.mjs'
    if (-not $node) {
        return $false
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Root 'captcha_happy.mjs'))) {
        return $false
    }
    if (-not (Test-Path -LiteralPath $probeScript)) {
        return $false
    }

    $probeExit = Invoke-NativeCommand -FilePath $node.Source -ArgumentList @($probeScript) -Quiet
    return $probeExit -eq 0
}

function Ensure-HappyDom {
    if (Test-HappyDom) {
        return $true
    }

    $node = Get-Command node -ErrorAction SilentlyContinue
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $node -or -not $npm -or -not (Test-Path -LiteralPath (Join-Path $Root 'package.json'))) {
        return $false
    }

    Write-Host '[glm2api] happy-dom solver was not found; installing the local Node dependency...' -ForegroundColor Yellow
    $installExit = Invoke-NativeCommand -FilePath $npm.Source -ArgumentList @(
        'install',
        '--omit=dev',
        '--no-audit',
        '--no-fund',
        '--no-package-lock'
    )
    if ($installExit -ne 0) {
        return $false
    }

    return (Test-HappyDom)
}

function Test-ExistingGlm2Api {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8008/healthz' -UseBasicParsing -TimeoutSec 1
        $status = $response.Content | ConvertFrom-Json
        return [bool]$status.ok -and $status.service -eq 'glm2api'
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
$CaptchaMode = 'happydom'
if (Ensure-HappyDom) {
    Write-Host '[glm2api] captcha solver: happy-dom (no browser worker required).' -ForegroundColor Green
} else {
    Write-Host '[glm2api] happy-dom is unavailable; enabling the Playwright browser fallback.' -ForegroundColor Yellow
    Ensure-Playwright -PythonExe $PythonExe
    $CaptchaMode = 'browser'
}

Write-Host ''
Write-Host '[glm2api] starting local web service...' -ForegroundColor Cyan
if ($HarPath) {
    Write-Host ('[glm2api] preload HAR: ' + $HarPath)
} else {
    Write-Host '[glm2api] no HAR found; starting without account. Add Token/HAR in the web panel.' -ForegroundColor Yellow
}
Write-Host '[glm2api] Web: http://127.0.0.1:8008/'
Write-Host '[glm2api] Login: Token/HAR are browser-free; Browser Login uses optional Playwright.'
Write-Host ''

$RunArgs = @('.\glm2api.py', '--serve', '--port', '8008', '--fresh-captcha', '--captcha-mode', $CaptchaMode, '--open-web')
if ($HarPath) {
    $RunArgs += @('--har', $HarPath)
}

& $PythonExe @RunArgs
