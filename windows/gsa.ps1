function Assert-GsaReady {
    Add-Type -AssemblyName System.ServiceProcess
    $required = @('GlobalSecureAccessEngineService', 'GlobalSecureAccessTunnelingService')
    foreach ($name in $required) {
        if (-not (Get-Process -Name $name -ErrorAction SilentlyContinue)) {
            throw 'Windows GSA engine/tunneling runtime is not running. Open GSA and sign in first.'
        }
    }
    $driver = New-Object System.ServiceProcess.ServiceController('GlobalSecureAccessDriver')
    try {
        $driver.Refresh()
        if ($driver.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Running) {
            throw 'The GSA network driver is not running. Repair GSA using your approved IT procedure.'
        }
    } finally {
        $driver.Dispose()
    }
}
