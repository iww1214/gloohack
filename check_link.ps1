# Checks each link from aircraft to dashboard in order and stops at the first one that is broken.
# Run:  Get-Content .\check_link.ps1 -Raw | Invoke-Expression      (or: powershell -File .\check_link.ps1)
$ErrorActionPreference = "Continue"
$root = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$net = Get-Content "$root\network.json" -Raw | ConvertFrom-Json
$phone = $net.phone_ip
$out = New-Object System.Collections.ArrayList

function Step($name, $ok, $detail, $fix) {
    $line = if ($ok) { "PASS  $name  $detail" } else { "FAIL  $name  $detail`n      FIX: $fix" }
    [void]$out.Add($line); Write-Host $line
    return [bool]$ok
}
function Get-Json($url, $sec) {
    try { return (Invoke-WebRequest $url -UseBasicParsing -TimeoutSec $sec).Content | ConvertFrom-Json } catch { return $null }
}

$mine = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -eq $net.laptop_ip } | Select-Object -First 1)
$ok = Step "1 Laptop on the hotspot" ($null -ne $mine) "laptop_ip=$($net.laptop_ip)" "Join the phone's hotspot, or update laptop_ip/phone_ip in network.json."

if ($ok) { $ok = Step "2 Phone reachable" (Test-Connection $phone -Count 2 -Quiet) $phone "Phone must be on the same hotspot with the screen on." }

$st = $null
if ($ok) {
    $st = Get-Json "http://${phone}:8080/status" 4
    $ok = Step "3 Control app answering" ($null -ne $st) "http://${phone}:8080/status" "Open the DJI Control Server app and keep it in the foreground."
}

if ($ok) {
    $linked = ($st.aircraft_connected -eq $true) -and (($null -ne $st.heading_deg) -or ($null -ne $st.altitude_m))
    $ok = Step "4 Aircraft linked to remote" $linked "aircraft_connected=$($st.aircraft_connected) heading=$($st.heading_deg) alt=$($st.altitude_m)" "Relink: remote ON first, then aircraft (flat surface, props clear), then reopen the app."
}

if ($ok) {
    $a = Get-Json "http://${phone}:8080/stream" 4
    Start-Sleep -Seconds 3
    $b = Get-Json "http://${phone}:8080/stream" 4
    $flow = ($null -ne $a) -and ($null -ne $b) -and ($b.frames -gt $a.frames)
    $ok = Step "5 Phone producing video" $flow "frames $($a.frames) -> $($b.frames)  [$($b.detail)]" "Aircraft link is up but no camera frames: force-close and reopen the app; if still 0, relink remote and aircraft."
}

if ($ok) {
    $conn = Get-NetTCPConnection -LocalPort 1935 -State Established -ErrorAction SilentlyContinue | Where-Object { $_.RemoteAddress -eq $phone }
    $ok = Step "6 Phone publishing to laptop" ($null -ne $conn) "RTMP :1935 from $phone" "Streaming server not receiving: check mediamtx is running; the app reconnects on its own within a few seconds."
}

if ($ok) {
    $d1 = Get-Json "http://127.0.0.1:8092/api/status" 5
    Start-Sleep -Seconds 4
    $d2 = Get-Json "http://127.0.0.1:8092/api/status" 5
    $demo = ($null -ne $d2) -and ($d2.demo_mode -eq $true)
    $flow = ($null -ne $d1) -and ($null -ne $d2) -and ($d2.frames_analyzed -gt $d1.frames_analyzed)
    if ($demo) { $ok = Step "7 Dashboard receiving video" $false "a demo replay is active" "Click 'Live drone view' on the page." }
    else { $ok = Step "7 Dashboard receiving video" $flow "frames_analyzed $($d1.frames_analyzed) -> $($d2.frames_analyzed)" "Dashboard is not pulling frames: restart start_demo.ps1." }
}

$final = if ($ok) { "ALL LINKS GOOD - live feed is ready." } else { "STOPPED at the first broken link above." }
[void]$out.Add($final); Write-Host $final
$out | Out-File "$env:TEMP\link_check.txt" -Encoding utf8
