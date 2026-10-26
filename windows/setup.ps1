param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('check', 'install')]
    [string]$Action,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-f0-9]{16}$')]
    [string]$DistroKey
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'gsa.ps1')
try {
    Assert-GsaReady
    $folder = Join-Path (Join-Path $env:LOCALAPPDATA 'WslGsaBridge') $DistroKey
    if ($Action -eq 'check') {
        @{ ready = $true; directory = $folder } | ConvertTo-Json -Compress
        exit 0
    }
    $node = Get-Command node.exe -CommandType Application -ErrorAction SilentlyContinue
    if (-not $node) {
        $winget = Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue
        if (-not $winget) {
            throw 'Node.js 22+ is required on Windows. Install the official LTS runtime, then retry.'
        }
        & $winget.Source install --id OpenJS.NodeJS.LTS --exact --source winget `
            --accept-source-agreements --accept-package-agreements --silent --disable-interactivity |
            Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Node.js installation failed. Install the official LTS runtime manually, then retry.'
        }
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
            [Environment]::GetEnvironmentVariable('Path', 'User')
        $node = Get-Command node.exe -CommandType Application -ErrorAction SilentlyContinue
        if (-not $node) { throw 'Node.js installed but node.exe is not discoverable. Reopen WSL and retry.' }
    }
    $version = & $node.Source -p 'process.versions.node'
    if ($LASTEXITCODE -ne 0 -or [int]($version.Split('.')[0]) -lt 22) {
        throw 'Windows Node.js 22+ is required. Upgrade Node.js and retry.'
    }
    if (Test-Path -LiteralPath (Join-Path $folder 'control.ps1')) {
        $status = & (Join-Path $folder 'control.ps1') -Action status | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect installed Windows bridge.' }
        if ($status.running) { throw 'Bridge is running. Run gsa off before reinstalling.' }
    }
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    foreach ($name in @('proxy.cjs', 'control.ps1', 'gsa.ps1')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $folder $name) -Force
    }
    [IO.File]::WriteAllText((Join-Path $folder 'node-path.txt'), $node.Source)
    @{ installed = $true; directory = $folder } | ConvertTo-Json -Compress
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
