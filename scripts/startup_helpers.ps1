function Invoke-NativeCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string] $FilePath,

        [string[]] $ArgumentList = @(),

        [switch] $Quiet
    )

    # Windows PowerShell 5 converts native stderr into ErrorRecord objects.
    # With the launcher's global ErrorActionPreference=Stop, an expected probe
    # failure would otherwise terminate the whole startup script before its
    # exit code can be inspected.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        if ($Quiet) {
            & $FilePath @ArgumentList *> $null
        } else {
            & $FilePath @ArgumentList 2>&1 | Out-Host
        }
        $exitCode = $LASTEXITCODE
        if ($null -eq $exitCode) {
            return 0
        }
        return [int] $exitCode
    } catch {
        if (-not $Quiet) {
            Write-Host ('[glm2api] failed to launch native command: ' + $_.Exception.Message) -ForegroundColor Red
        }
        return 1
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}
