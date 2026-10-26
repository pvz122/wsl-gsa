param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('check', 'start', 'stop', 'status', 'uninstall')]
    [string]$Action,
    [string]$HostAddress
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'gsa.ps1')
$statePath = Join-Path $PSScriptRoot 'state.json'
$proxyPath = Join-Path $PSScriptRoot 'proxy.cjs'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$mutex = [Threading.Mutex]::new($false, "Local\WslGsaBridge-$sid")
$locked = $false

function Get-ProxyState {
    if (-not (Test-Path -LiteralPath $statePath)) { return $null }
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$state.pid)"
    if (-not $process) { return $null }
    if ($process.Name -ne 'node.exe' -or -not $process.CommandLine -or
        -not $process.CommandLine.Contains("`"$proxyPath`"") -or
        $process.CreationDate.ToUniversalTime().ToString('o') -ne $state.created) {
        throw 'State does not match the managed Node process. Refusing to stop a potentially reused PID.'
    }
    return $state
}

try {
    $locked = $mutex.WaitOne(15000)
    if (-not $locked) { throw 'Another Windows bridge command is still running.' }
    if ($Action -eq 'check') {
        Assert-GsaReady
        @{ ready = $true } | ConvertTo-Json -Compress
        exit 0
    }
    $state = Get-ProxyState
    if ($Action -eq 'stop' -or $Action -eq 'uninstall') {
        $retained = @()
        if ($state) {
            Stop-Process -Id ([int]$state.pid) -ErrorAction Stop
            Wait-Process -Id ([int]$state.pid) -Timeout 10 -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $statePath) { Remove-Item -LiteralPath $statePath }
        if ($Action -eq 'uninstall') {
            foreach ($name in @('proxy.cjs', 'gsa.ps1', 'node-path.txt', 'bridge.log', 'control.ps1')) {
                $file = Join-Path $PSScriptRoot $name
                if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file }
            }
            if (@(Get-ChildItem -LiteralPath $PSScriptRoot -Force).Count) {
                $retained += $PSScriptRoot
            } else { Remove-Item -LiteralPath $PSScriptRoot }
        }
        @{ running = $false; retained_paths = $retained } | ConvertTo-Json -Compress
    } elseif ($Action -eq 'status') {
        if ($state) {
            @{ running = $true; pid = $state.pid; host = $state.host; port = 18080 } |
                ConvertTo-Json -Compress
        } else { @{ running = $false } | ConvertTo-Json -Compress }
    } else {
        Assert-GsaReady
        $address = $null
        if (-not [Net.IPAddress]::TryParse($HostAddress, [ref]$address) -or
            $address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
            throw 'A valid WSL NAT gateway IPv4 address is required.'
        }
        $adapter = [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() |
            Where-Object { $_.Name -like '*WSL*' } |
            ForEach-Object { $_.GetIPProperties().UnicastAddresses } |
            Where-Object { $_.Address.ToString() -eq $HostAddress }
        if (-not $adapter) { throw 'Gateway is not a Windows WSL adapter. NAT networking is required.' }
        if ($state -and $state.host -ne $HostAddress) {
            throw 'Gateway changed. Run gsa off before gsa on.'
        }
        if (-not $state) {
            $node = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'node-path.txt') -Raw).Trim()
            if (-not (Test-Path -LiteralPath $node)) { throw 'Saved Node.js runtime missing. Reinstall the bridge.' }
            $listeners = @([Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().
                GetActiveTcpListeners() | Where-Object { $_.Port -eq 18080 -and
                    $_.Address.ToString() -in @($HostAddress, '0.0.0.0', '::') })
            if ($listeners.Count) {
                throw 'TCP 18080 is already in use. Stop its owner (possibly another distro); nothing was replaced.'
            }
            & $node --check $proxyPath
            if ($LASTEXITCODE -ne 0) { throw 'Windows proxy syntax check failed.' }
            [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'bridge.log'), '')
            # No redirected standard streams: WSL must not wait on inherited pipe handles.
            $child = Start-Process -FilePath $node -ArgumentList @("`"$proxyPath`"", $HostAddress) `
                -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
            try {
                Start-Sleep -Milliseconds 350
                $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($child.Id)"
                if (-not $process) { throw 'Windows bridge exited. Inspect bridge.log in its installation folder.' }
                $state = @{ pid = $child.Id; host = $HostAddress
                    created = $process.CreationDate.ToUniversalTime().ToString('o') }
                [IO.File]::WriteAllText($statePath, ($state | ConvertTo-Json -Compress))
            } catch {
                if (-not $child.HasExited) { $child.Kill() }
                throw
            }
        }
        @{ running = $true; pid = $state.pid; host = $state.host; port = 18080 } |
            ConvertTo-Json -Compress
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
