# Prepare only the Windows artifact after local gates. Publication and other
# platform/signing checks remain in the repository's existing release workflow.
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidatePattern('^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')] [string] $Tag,
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Invoke-Checked {
    param([string] $Command, [string[]] $ArgumentList)
    $global:LASTEXITCODE = 0
    $output = & $Command @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$Command failed with exit code $LASTEXITCODE."
    }
    return $output
}

function Assert-Repository {
    param([string] $ExpectedRevision = '')
    $status = @(Invoke-Checked 'git' @('status', '--porcelain'))
    if ($status.Count -gt 0) { throw 'Working tree is not clean.' }
    $branch = Invoke-Checked 'git' @('branch', '--show-current')
    if ($branch -ne 'main') { throw 'Release preparation requires main.' }
    Invoke-Checked 'git' @('fetch', '--quiet', 'origin', 'main') | Out-Host
    $head = Invoke-Checked 'git' @('rev-parse', 'HEAD')
    $upstream = Invoke-Checked 'git' @('rev-parse', 'origin/main')
    if ($head -ne $upstream) { throw 'HEAD is not origin/main.' }
    if ($ExpectedRevision -and $head -ne $ExpectedRevision) {
        throw 'The candidate changed during release preparation.'
    }
    return $head
}

function Assert-Version {
    if ((Get-DeclaredVersion) -ne $Tag.Substring(1)) {
        throw "Tag $Tag does not match the declared version."
    }
}

function Assert-Artifact {
    param([string] $Path, [datetime] $Started)
    $artifact = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($artifact.PSIsContainer -or $artifact.Length -eq 0 -or
        $artifact.LastWriteTimeUtc -lt $Started.AddSeconds(-2)) {
        throw "Missing, empty or stale release artifact: $Path"
    }
    Get-FileHash -LiteralPath $Path -Algorithm SHA256 | Format-List
}

function Get-DeclaredVersion {
    $content = Get-Content -Raw -LiteralPath 'source/snipvoice.pyw'
    if ($content -notmatch '(?m)^Version: ([0-9]+\.[0-9]+\.[0-9]+)\r?$') {
        throw 'Source has no release version.'
    }
    $declared = $Matches[1]
    if ($content -notmatch '(?m)^Channel: stable\r?$') { throw 'A stable release is required.' }
    $installer = Get-Content -Raw -LiteralPath 'installer/snipvoice.iss'
    $workflow = Get-Content -Raw -LiteralPath '.github/workflows/bundles.yml'
    $checks = @(
        @{ Text = $content; Pattern = '(?m)^APP_VERSION = "([^"]+)"'; Expected = $declared; Label = 'runtime version' },
        @{ Text = $content; Pattern = '(?m)^RELEASE_CHANNEL = "([^"]+)"'; Expected = 'stable'; Label = 'runtime channel' },
        @{ Text = $installer; Pattern = '(?m)^#define MyAppVersion "([^"]+)"'; Expected = $declared; Label = 'installer version' },
        @{ Text = $installer; Pattern = '(?m)^#define MyAppChannel "([^"]+)"'; Expected = 'stable'; Label = 'installer channel' },
        @{ Text = $workflow; Pattern = '(?m)^  SNIPVOICE_VERSION: "([^"]+)"'; Expected = $declared; Label = 'workflow version' },
        @{ Text = $workflow; Pattern = '(?m)^  SNIPVOICE_CHANNEL: "([^"]+)"'; Expected = 'stable'; Label = 'workflow channel' },
        @{ Text = $workflow; Pattern = '(?m)^  SNIPVOICE_RELEASE_LABEL: "([^"]+)"'; Expected = $declared; Label = 'release label' }
    )
    foreach ($check in $checks) {
        if ($check.Text -notmatch $check.Pattern -or $Matches[1] -ne $check.Expected) {
            throw "Release $($check.Label) does not match the declared stable version."
        }
    }
    return $declared
}

$previousRustFlags = $env:RUSTFLAGS
$previousSniptypeMode = $env:SNIPTYPE_BUILD_NONINTERACTIVE
$previousSnipvoiceMode = $env:SNIPVOICE_BUILD_NONINTERACTIVE
Push-Location $PSScriptRoot
try {
    if (-not $IsWindows) { throw 'Use Windows for this preparation path.' }
    if (-not (Get-Command 'python' -ErrorAction SilentlyContinue)) {
        throw 'python is required; use the existing declared release environment.'
    }
    Assert-Version
    $candidate = Assert-Repository
    Invoke-Checked 'python' @('-m', 'unittest', 'discover', '-s', 'source/tests', '-v') | Out-Host
    Assert-Version
    Assert-Repository $candidate | Out-Null
    if ($DryRun) {
        Write-Host "DRY RUN: gates passed for $candidate. Would invoke the compliance-gated package and installer builders."
        Write-Host 'No builder, artifact promotion, tag, push or publication ran.'
        return
    }
    $version = $Tag.Substring(1)
    $started = [datetime]::UtcNow
    if (-not $env:SNIPVOICE_FFMPEG_COMPLIANCE_DIR -or
        -not (Test-Path -LiteralPath (Join-Path $env:SNIPVOICE_FFMPEG_COMPLIANCE_DIR 'runtime-manifest.json') -PathType Leaf)) {
        throw 'SNIPVOICE_FFMPEG_COMPLIANCE_DIR must contain runtime-manifest.json.'
    }
    $env:SNIPVOICE_BUILD_NONINTERACTIVE = '1'
    Invoke-Checked 'cmd' @('/c', 'build_release.bat') | Out-Host
    Invoke-Checked 'cmd' @('/c', 'build_installer.bat') | Out-Host
    Assert-Artifact "installer/Output/SnipvoiceSetup-$version.exe" $started
    Assert-Version
    Assert-Repository $candidate | Out-Null
    Write-Host "Prepared Windows artifact for $Tag at $candidate."
    Write-Host 'Other platform, signing and publication gates remain required.'
}
finally {
    $env:RUSTFLAGS = $previousRustFlags
    $env:SNIPTYPE_BUILD_NONINTERACTIVE = $previousSniptypeMode
    $env:SNIPVOICE_BUILD_NONINTERACTIVE = $previousSnipvoiceMode
    Pop-Location
}
