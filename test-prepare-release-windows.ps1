# Standalone behavior tests; every command is fake and every file is temporary.
$ErrorActionPreference = 'Stop'
$original = Get-Location
$sandbox = Join-Path $env:TEMP ('release-test-' + [guid]::NewGuid().ToString('N'))
$previousTestState = Get-Variable -Name 'releasePreparationTestState' -Scope Global -ErrorAction SilentlyContinue
$global:releasePreparationTestState = @{ Mode = 'ok'; Gates = 0 }
try {
    New-Item -ItemType Directory -Path $sandbox | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $sandbox 'source') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $sandbox 'installer') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $sandbox '.github/workflows') -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'prepare-release-windows.ps1') -Destination $sandbox
    'Version: 1.2.3
Channel: stable
APP_VERSION = "1.2.3"
RELEASE_CHANNEL = "stable"
' | Set-Content -LiteralPath (Join-Path $sandbox 'source/snipvoice.pyw')
    '#define MyAppVersion "1.2.3"
#define MyAppChannel "stable"
' | Set-Content -LiteralPath (Join-Path $sandbox 'installer/snipvoice.iss')
    'env:
  SNIPVOICE_VERSION: "1.2.3"
  SNIPVOICE_CHANNEL: "stable"
  SNIPVOICE_RELEASE_LABEL: "1.2.3"
' | Set-Content -LiteralPath (Join-Path $sandbox '.github/workflows/bundles.yml')

    function git {
        param([Parameter(ValueFromRemainingArguments)] $ArgumentList)
        $global:LASTEXITCODE = 0
        switch ($ArgumentList[0]) {
            'status' {
                if ($global:releasePreparationTestState.Mode -eq 'dirty' -or
                    ($global:releasePreparationTestState.Mode -eq 'gate-dirty' -and $global:releasePreparationTestState.Gates -gt 0)) {
                    ' M example'
                }
            }
            'branch' { if ($global:releasePreparationTestState.Mode -eq 'branch') { 'topic' } else { 'main' } }
            'fetch' { if ($global:releasePreparationTestState.Mode -eq 'fetch-fail') { $global:LASTEXITCODE = 9 } }
            'rev-parse' {
                if ($global:releasePreparationTestState.Mode -eq 'upstream' -and $ArgumentList[1] -eq 'origin/main') {
                    'different'
                } else { 'same' }
            }
            default { throw "Unexpected Git command: $ArgumentList" }
        }
    }

    function python {
        $global:releasePreparationTestState.Gates += 1
        $global:LASTEXITCODE = if ($global:releasePreparationTestState.Mode -eq 'gate-fail') { 8 } else { 0 }
        if ($global:releasePreparationTestState.Mode -eq 'metadata-after-tests') {
            '#define MyAppVersion "9.9.9"' | Set-Content -LiteralPath (Join-Path $sandbox 'installer/snipvoice.iss')
        }
    }

    function cmd { throw 'A dry run must never invoke a package builder.' }

    & (Join-Path $sandbox 'prepare-release-windows.ps1') -Tag v1.2.3 -DryRun
    if ($global:releasePreparationTestState.Gates -eq 0) { throw 'Dry run skipped native gates.' }

    $cases = @{
        'dirty' = 'clean'
        'branch' = 'main'
        'upstream' = 'origin/main'
        'fetch-fail' = 'failed'
        'gate-fail' = 'failed'
        'gate-dirty' = 'clean'
    }
    foreach ($case in $cases.GetEnumerator()) {
        $global:releasePreparationTestState.Mode = $case.Key
        $global:releasePreparationTestState.Gates = 0
        $caught = $false
        try {
            & (Join-Path $sandbox 'prepare-release-windows.ps1') -Tag v1.2.3 -DryRun
        } catch {
            if ($_.Exception.Message -notmatch $case.Value) { throw }
            $caught = $true
        }
        if (-not $caught) { throw "Invalid release condition was accepted: $($case.Key)" }
        if ($case.Key -in @('dirty', 'branch', 'upstream', 'fetch-fail') -and $global:releasePreparationTestState.Gates -ne 0) {
            throw "A rejected checkout ran gates: $($case.Key)"
        }
    }
    $metadataCases = @(
        @{ Path = 'source/snipvoice.pyw'; Before = 'APP_VERSION = "1.2.3"'; After = 'APP_VERSION = "9.9.9"'; Error = 'runtime version' },
        @{ Path = 'source/snipvoice.pyw'; Before = 'RELEASE_CHANNEL = "stable"'; After = 'RELEASE_CHANNEL = "beta"'; Error = 'runtime channel' },
        @{ Path = 'installer/snipvoice.iss'; Before = 'MyAppVersion "1.2.3"'; After = 'MyAppVersion "9.9.9"'; Error = 'installer version' },
        @{ Path = 'installer/snipvoice.iss'; Before = 'MyAppChannel "stable"'; After = 'MyAppChannel "beta"'; Error = 'installer channel' },
        @{ Path = '.github/workflows/bundles.yml'; Before = 'SNIPVOICE_VERSION: "1.2.3"'; After = 'SNIPVOICE_VERSION: "9.9.9"'; Error = 'workflow version' },
        @{ Path = '.github/workflows/bundles.yml'; Before = 'SNIPVOICE_CHANNEL: "stable"'; After = 'SNIPVOICE_CHANNEL: "beta"'; Error = 'workflow channel' },
        @{ Path = '.github/workflows/bundles.yml'; Before = 'SNIPVOICE_RELEASE_LABEL: "1.2.3"'; After = 'SNIPVOICE_RELEASE_LABEL: "9.9.9"'; Error = 'release label' }
    )
    foreach ($case in $metadataCases) {
        $global:releasePreparationTestState.Mode = 'ok'
        $global:releasePreparationTestState.Gates = 0
        $path = Join-Path $sandbox $case.Path
        $before = Get-Content -Raw -LiteralPath $path
        $before.Replace($case.Before, $case.After) | Set-Content -LiteralPath $path
        $caught = $false
        try {
            & (Join-Path $sandbox 'prepare-release-windows.ps1') -Tag v1.2.3 -DryRun
        } catch {
            if ($_.Exception.Message -notmatch $case.Error) { throw }
            $caught = $true
        } finally {
            $before | Set-Content -LiteralPath $path
        }
        if (-not $caught) { throw "Mismatched release metadata was accepted: $($case.Path) $($case.Before)" }
        if ($global:releasePreparationTestState.Gates -ne 0) { throw 'Mismatched metadata ran test gates.' }
    }
    $global:releasePreparationTestState.Mode = 'metadata-after-tests'
    $global:releasePreparationTestState.Gates = 0
    $caught = $false
    try {
        & (Join-Path $sandbox 'prepare-release-windows.ps1') -Tag v1.2.3 -DryRun
    } catch {
        if ($_.Exception.Message -notmatch 'installer version') { throw }
        $caught = $true
    }
    if (-not $caught) { throw 'Metadata drift during tests was accepted.' }
    Write-Host 'snipvoice: 15 release preparation scenarios passed'
}
finally {
    Set-Location $original
    if ($previousTestState) {
        Set-Variable -Name 'releasePreparationTestState' -Scope Global -Value $previousTestState.Value
    } else {
        Remove-Variable -Name 'releasePreparationTestState' -Scope Global
    }
    $resolved = [IO.Path]::GetFullPath($sandbox)
    $tempRoot = [IO.Path]::GetFullPath($env:TEMP) + [IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($tempRoot) -or -not (Split-Path $resolved -Leaf).StartsWith('release-test-')) {
        throw 'Refusing unsafe test cleanup.'
    }
    if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
$global:LASTEXITCODE = 0
