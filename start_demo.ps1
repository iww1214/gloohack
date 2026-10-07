$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = (Get-Command python -ErrorAction Stop).Source
$net = Get-Content "$PSScriptRoot\network.json" -Raw | ConvertFrom-Json
foreach ($address in @($net.laptop_ip, $net.phone_ip)) {
    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse([string]$address, [ref]$parsed)) {
        throw "Configure valid laptop_ip and phone_ip values in network.json."
    }
}
$streamExe = Join-Path $PSScriptRoot "mediamtx_bin\mediamtx.exe"
$streamConfig = Join-Path $PSScriptRoot "mediamtx.live.yml"
if (-not (Test-Path $streamExe) -or -not (Test-Path $streamConfig)) {
    throw "Install MediaMTX and configure mediamtx.live.yml before starting."
}
if ((Get-Content $streamConfig -Raw) -match "CONFIGURE_A_UNIQUE_LOCAL_PASSWORD") {
    throw "Replace the streaming password placeholder before starting."
}
foreach ($port in @(1935, 8554, 8092)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "Port $port is already in use. Existing services were left untouched; do not restart while the phone is publishing."
    }
}
$env:PYTHONIOENCODING = "utf-8"
$env:MSDK_BRIDGE_URL = "http://$($net.phone_ip):8080"
$env:SAFETY1271_VIDEO_MODE = "live"
$env:RTMP_STREAM_URL = "rtsp://127.0.0.1:8554/live/drone"
$stream = Start-Process -FilePath $streamExe -ArgumentList "`"$streamConfig`"" -WorkingDirectory $PSScriptRoot -PassThru
$dashboard = Start-Process -FilePath $py -ArgumentList "-m uvicorn dashboard_server:app --host 127.0.0.1 --port 8092" -WorkingDirectory $PSScriptRoot -PassThru
Start-Sleep -Seconds 6
if ($stream.HasExited -or $dashboard.HasExited) {
    throw "A newly started service exited. Inspect its console output before opening the app."
}
$null = Invoke-WebRequest "http://127.0.0.1:8092/api/status" -UseBasicParsing -TimeoutSec 5
Write-Output "Streaming server PID: $($stream.Id); dashboard PID: $($dashboard.Id)"
Write-Output "Dashboard: http://127.0.0.1:8092/"
Write-Output "Start the remote, aircraft, and phone app next. Run check_link.ps1 to verify the complete link."
