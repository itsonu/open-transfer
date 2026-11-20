# M0 spike (throwaway): what this Windows laptop can do for the four scenarios.
# PowerShell 5.1 (built in) and WinRT only; nothing to install.
#
#   .\m0.ps1 caps                      Bluetooth roles, hosted-network support, upstream connection
#   .\m0.ps1 wfd-host [ssid] [pass]    Wi-Fi Direct legacy access point (phones join it as Wi-Fi)
#   .\m0.ps1 hotspot                   Mobile Hotspot (may need an upstream connection)
#   .\m0.ps1 join <ssid> <pass>        join a network from a per-user profile (scenario S1)
#   .\m0.ps1 leave <ssid>              forget it
#   .\m0.ps1 sink                      TCP sink on :47900 (same protocol as tcp_speed.py)
#   .\m0.ps1 send <host> [MB]          stream MB to host:47900
param([string]$Command = "", [string]$A = "", [string]$B = "")
$ErrorActionPreference = "Stop"
$Port = 47900

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await($op, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}
function Say($text) { Write-Output ("[{0:HH:mm:ss}] {1}" -f (Get-Date), $text) }

function Caps {
    [Windows.Devices.Bluetooth.BluetoothAdapter, Windows.Devices.Bluetooth, ContentType = WindowsRuntime] | Out-Null
    $bt = Await ([Windows.Devices.Bluetooth.BluetoothAdapter]::GetDefaultAsync()) ([Windows.Devices.Bluetooth.BluetoothAdapter])
    if ($bt) {
        Say "Bluetooth LE: $($bt.IsLowEnergySupported); central: $($bt.IsCentralRoleSupported); peripheral (advertise + GATT server): $($bt.IsPeripheralRoleSupported)"
    } else { Say "Bluetooth: no adapter" }
    Say "Hosted network (old netsh hotspot):"
    netsh wlan show drivers | Select-String -Pattern "Hosted network supported|Wi-Fi Direct|Radio types"
    [Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType = WindowsRuntime] | Out-Null
    $p = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
    Say "Upstream (internet) connection: $(if ($p) { $p.ProfileName } else { 'none' })"
    Say "Current Wi-Fi (Windows 11 may hide the name without Location):"
    netsh wlan show interfaces | Select-String -Pattern "^\s+(SSID|State|Radio type)\s"
}

function WfdHost([string]$ssid, [string]$pass) {
    if (-not $ssid) { $ssid = "DIRECT-OT-M0" }
    if (-not $pass) { $pass = -join ((48..57) + (97..122) | Get-Random -Count 12 | ForEach-Object { [char]$_ }) }
    [Windows.Devices.WiFiDirect.WiFiDirectAdvertisementPublisher, Windows.Devices.WiFiDirect, ContentType = WindowsRuntime] | Out-Null
    [Windows.Security.Credentials.PasswordCredential, Windows.Security.Credentials, ContentType = WindowsRuntime] | Out-Null
    $pub = [Windows.Devices.WiFiDirect.WiFiDirectAdvertisementPublisher]::new()
    $pub.Advertisement.IsAutonomousGroupOwnerEnabled = $true
    $pub.Advertisement.LegacySettings.IsEnabled = $true
    $pub.Advertisement.LegacySettings.Ssid = $ssid
    $cred = [Windows.Security.Credentials.PasswordCredential]::new()
    $cred.Password = $pass
    $pub.Advertisement.LegacySettings.Passphrase = $cred
    $pub.Start()
    Start-Sleep -Seconds 2
    Say "Wi-Fi Direct legacy AP status: $($pub.Status)  ssid=$ssid  passphrase=$pass"
    Say "Wi-Fi QR: WIFI:T:WPA;S:$ssid;P:$pass;;"
    Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -like "*Direct*" -or $_.InterfaceAlias -like "Local Area Connection*" } |
        ForEach-Object { Say "  address $($_.IPAddress) on $($_.InterfaceAlias)" }
    Say "Keeping it up; Ctrl+C to stop."
    try { while ($true) { Start-Sleep -Seconds 5; if ($pub.Status -ne "Started") { Say "status now $($pub.Status)"; break } } }
    finally { $pub.Stop(); Say "stopped" }
}

function Hotspot {
    [Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType = WindowsRuntime] | Out-Null
    [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager, Windows.Networking.NetworkOperators, ContentType = WindowsRuntime] | Out-Null
    $p = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
    if (-not $p) {
        Say "No upstream connection profile: Mobile Hotspot has nothing to share from (record this result)."
        $p = [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles() | Select-Object -First 1
        if (-not $p) { return }
        Say "Trying with profile '$($p.ProfileName)' instead"
    }
    $tm = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($p)
    Say "Tethering state before: $($tm.TetheringOperationalState); capability: $([Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::GetTetheringCapabilityFromConnectionProfile($p))"
    $r = Await ($tm.StartTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
    $cfg = $tm.GetCurrentAccessPointConfiguration()
    Say "Start: $($r.Status) $($r.AdditionalErrorMessage)  ssid=$($cfg.Ssid)  passphrase=$($cfg.Passphrase)"
    Say "Press Enter to stop the hotspot."
    Read-Host | Out-Null
    Await ($tm.StopTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult]) | Out-Null
    Say "stopped"
}

function Join([string]$ssid, [string]$pass) {
    $hex = -join ([Text.Encoding]::UTF8.GetBytes($ssid) | ForEach-Object { $_.ToString("X2") })
    $xml = @"
<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>$ssid</name>
  <SSIDConfig><SSID><hex>$hex</hex><name>$ssid</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType><connectionMode>manual</connectionMode>
  <MSM><security>
    <authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption><useOneX>false</useOneX></authEncryption>
    <sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>$pass</keyMaterial></sharedKey>
  </security></MSM>
</WLANProfile>
"@
    $file = Join-Path $env:TEMP "ot-m0-profile.xml"
    Set-Content -Path $file -Value $xml -Encoding UTF8
    netsh wlan add profile filename="$file" user=current
    Remove-Item $file
    netsh wlan connect name="$ssid"
    Start-Sleep -Seconds 6
    Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } |
        ForEach-Object { Say "$($_.InterfaceAlias): $($_.IPv4Address.IPAddress) gateway $($_.IPv4DefaultGateway.NextHop)" }
}

function Leave([string]$ssid) {
    netsh wlan disconnect
    netsh wlan delete profile name="$ssid"
    Say "Forgot $ssid; Windows rejoins a known network by itself."
}

function Sink {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, $Port)
    $listener.Start()
    Say "TCP sink listening on :$Port"
    $buf = New-Object byte[] (256KB)
    while ($true) {
        $client = $listener.AcceptTcpClient()
        $stream = $client.GetStream()
        $watch = [Diagnostics.Stopwatch]::StartNew()
        $total = 0L
        while (($n = $stream.Read($buf, 0, $buf.Length)) -gt 0) { $total += $n }
        $ms = [Math]::Max($watch.ElapsedMilliseconds, 1)
        Say ("RECEIVED {0} MB from {1} in {2} ms = {3:N1} MB/s" -f [int]($total / 1MB), $client.Client.RemoteEndPoint, $ms, ($total / 1MB) / ($ms / 1000))
        $client.Close()
    }
}

function Send([string]$target, [int]$mb) {
    if ($mb -le 0) { $mb = 100 }
    $client = [Net.Sockets.TcpClient]::new($target, $Port)
    $stream = $client.GetStream()
    $buf = New-Object byte[] (256KB)
    $watch = [Diagnostics.Stopwatch]::StartNew()
    for ($left = [long]$mb * 1MB; $left -gt 0; $left -= $buf.Length) { $stream.Write($buf, 0, [Math]::Min($left, $buf.Length)) }
    $client.Client.Shutdown([Net.Sockets.SocketShutdown]::Send)
    $client.Close()
    $ms = [Math]::Max($watch.ElapsedMilliseconds, 1)
    Say ("SENT {0} MB to {1} in {2} ms = {3:N1} MB/s" -f $mb, $target, $ms, $mb / ($ms / 1000))
}

switch ($Command) {
    "caps" { Caps }
    "wfd-host" { WfdHost $A $B }
    "hotspot" { Hotspot }
    "join" { Join $A $B }
    "leave" { Leave $A }
    "sink" { Sink }
    "send" { Send $A ([int]$B) }
    default { Get-Content $PSCommandPath -TotalCount 11 | Select-Object -Skip 1 }
}
