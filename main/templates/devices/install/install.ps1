{% autoescape off %}# {{ site_name }}: connect this Windows computer.
#
# Downloaded from {{ server }} for one pairing code. It:
#   1. shows who will see this computer, and pairs only if you say yes;
#   2. installs the NexGuard agent from that dashboard as a protected service (Windows
#      asks an administrator to allow it): health (CPU, memory, GPU, battery, storage,
#      network, Windows version), and screen time, websites and website blocking if the
#      dashboard turns them on. Without an administrator, the same agent running as you
#      (no blocking); if the dashboard has no agent, a health-only check-in task;
#   3. checks in once so the dashboard shows the computer straight away.
#
# To remove it later:  irm {{ server }}/d/uninstall.ps1 | iex
$NgServer = '{{ server }}'
$NgCode = '{{ code }}'
{% endautoescape %}{% verbatim %}$NgVersion = '1.0.0'

& {
  $ErrorActionPreference = 'Stop'
  # Windows PowerShell 5.1 on older builds still defaults to TLS 1.0.
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

  $dir = Join-Path $env:LOCALAPPDATA 'NexGuard'
  $taskName = 'NexGuard device check-in'

  function Stop-Setup([string]$message) {
    Write-Host ''
    Write-Host "Couldn't connect this computer: $message" -ForegroundColor Red
    Write-Host ''
    throw 'NexGuard setup stopped.'
  }

  function Invoke-Pair([hashtable]$payload) {
    $json = $payload | ConvertTo-Json -Compress
    try {
      return Invoke-RestMethod -Method Post -Uri "$NgServer/device/v1/pair" -TimeoutSec 20 `
        -ContentType 'application/json; charset=utf-8' -UserAgent "nexguard-device-ps/$NgVersion" `
        -Body ([Text.Encoding]::UTF8.GetBytes($json))
    } catch {
      $detail = $null
      if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
        try { $detail = ($_.ErrorDetails.Message | ConvertFrom-Json).error } catch { $detail = $null }
      }
      if (-not $detail) { $detail = "can't reach $NgServer. Check the internet connection and try again." }
      Stop-Setup $detail
    }
  }

  Write-Host ''
  Write-Host "Connecting this computer to the dashboard at $NgServer"

  $uninstall = @'
# Stop NexGuard on this computer (the agent or the check-in task) and remove its files.
param([switch]$Quiet)
$dir = Join-Path $env:LOCALAPPDATA 'NexGuard'
foreach ($task in 'NexGuard device check-in', 'NexGuard agent') {
  Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
}
$exe = Join-Path $dir 'nexguard.exe'
Get-Process -Name nexguard -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe } |
  Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500  # let Windows release the program file
Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue
if (-not $Quiet) { Write-Host 'NexGuard is removed from this computer.' }
'@

  # ------------------------------------------------------------- the agent, if offered
  # The same program an administrator can install as a service, here running as you: it
  # can't block websites (that needs administrator rights), everything else works.
  $agentTask = 'NexGuard agent'
  $arch = ''
  if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { $arch = 'arm64' }
  elseif ([Environment]::Is64BitOperatingSystem) { $arch = 'amd64' }
  $agentTmp = $null
  if ($arch -and $env:NEXGUARD_NO_AGENT -ne '1') {
    $bin = "nexguard-windows-$arch.exe"
    $tmp = Join-Path $env:TEMP ('nexguard-' + [guid]::NewGuid().ToString() + '.exe')
    try {
      $ProgressPreference = 'SilentlyContinue'  # the progress bar makes 5.1 downloads crawl
      Invoke-WebRequest -UseBasicParsing -Uri "$NgServer/d/agent/$bin" -OutFile $tmp -TimeoutSec 300
      $sums = (Invoke-WebRequest -UseBasicParsing -Uri "$NgServer/d/agent/SHA256SUMS" -TimeoutSec 20).Content
      if ($sums -is [byte[]]) { $sums = [Text.Encoding]::ASCII.GetString($sums) }
      $want = $null
      foreach ($line in ($sums -split "`n")) {
        $f = $line.Trim() -split '\s+'
        if ($f.Count -ge 2 -and $f[1].TrimStart('*') -eq $bin) { $want = $f[0].ToLower() }
      }
      $got = (Get-FileHash -Algorithm SHA256 -Path $tmp).Hash.ToLower()
      if ($want -and $want -eq $got) { $agentTmp = $tmp }
      else { Write-Host "(The agent download didn't match its checksum: installing the check-in task.)" }
    } catch { $agentTmp = $null }
    if (-not $agentTmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
  }

  # --------------------------------------------- as an administrator: blocking included
  # The protected service: it can block websites, and someone without an administrator
  # can't stop or remove it. `nexguard install` copies itself to Program Files.
  if ($agentTmp -and $env:NEXGUARD_USER_INSTALL -ne '1') {
    Write-Host ''
    Write-Host 'NexGuard installs as a protected service, so it can also block websites.'
    Write-Host 'Windows asks next whether to allow it: choose Yes (an administrator account or'
    Write-Host 'password is needed). If you choose No, NexGuard installs for you only, with no blocking.'
    $exeQ = $agentTmp -replace "'", "''"
    # Exit codes: 0 installed, 1 not paired (said no, or a bad code), 2 service failed.
    $elevated = @"
& '$exeQ' pair '$NgServer' '$NgCode'
if (`$LASTEXITCODE -ne 0) { Read-Host 'Press Enter to close'; exit 1 }
& '$exeQ' install
if (`$LASTEXITCODE -ne 0) { Read-Host 'Press Enter to close'; exit 2 }
Start-Sleep -Seconds 3
exit 0
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($elevated))
    $result = $null
    try {
      $p = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded"
      $result = $p.ExitCode
    } catch {
      Write-Host ''
      Write-Host 'Not allowed as an administrator: installing for you only, without website blocking.'
    }
    if ($null -ne $result) {
      Remove-Item $agentTmp -Force -ErrorAction SilentlyContinue
      if ($result -eq 1) {
        Write-Host ''
        Write-Host 'Nothing was installed. If you said no, the code still works.'
        return
      }
      if ($result -ne 0) { Stop-Setup "it's paired, but the service didn't start. Paste the line again to retry." }
      # Replace what ran as you before (the check-in task, or the agent without blocking).
      $u = Join-Path $dir 'uninstall.ps1'
      if (Test-Path $u) { & $u -Quiet }
      Write-Host ''
      Write-Host 'Done. NexGuard runs as a protected service on this computer and can block websites.' -ForegroundColor Green
      Write-Host "You can close this window. To remove it later, paste:  irm $NgServer/d/uninstall.ps1 | iex"
      Write-Host ''
      return
    }
  }

  # ------------------------------------------- no administrator: the agent, running as you
  if ($agentTmp) {
    $newDir = -not (Test-Path $dir)
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $exe = Join-Path $dir 'nexguard.exe'
    # An earlier install: stop it so it neither holds the program file nor checks in
    # with the old script while the agent pairs (both keep config.json here).
    $oldTask = Get-ScheduledTask -TaskName 'NexGuard device check-in' -ErrorAction SilentlyContinue
    if ($oldTask) { Disable-ScheduledTask -TaskName 'NexGuard device check-in' | Out-Null }
    Stop-ScheduledTask -TaskName $agentTask -ErrorAction SilentlyContinue
    Get-Process -Name nexguard -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe } |
      Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    Move-Item -Force $agentTmp $exe

    # The agent shows what the dashboard will see (screen time and websites included, if
    # they can be turned on for this computer) and asks.
    $pairArgs = @('--user', '--dir', $dir, 'pair')
    if ($env:NEXGUARD_YES -eq '1') { $pairArgs += '--yes' }
    $pairArgs += @($NgServer, $NgCode)
    & $exe @pairArgs
    if ($LASTEXITCODE -ne 0) {
      if ($oldTask) { Enable-ScheduledTask -TaskName 'NexGuard device check-in' | Out-Null }
      Remove-Item $exe -Force -ErrorAction SilentlyContinue
      if ($newDir) { Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue }
      Write-Host ''
      Write-Host 'Nothing was installed. If you said no, the code still works.'
      return
    }

    # Replace the earlier check-in task, if this computer had one.
    Unregister-ScheduledTask -TaskName 'NexGuard device check-in' -Confirm:$false -ErrorAction SilentlyContinue
    foreach ($old in 'checkin.ps1', 'last', 'error') {
      Remove-Item (Join-Path $dir $old) -Force -ErrorAction SilentlyContinue
    }
    Set-Content -Path (Join-Path $dir 'uninstall.ps1') -Value $uninstall -Encoding ASCII

    # Runs at sign-in for as long as you're signed in, with no window; restarts if it stops.
    $agentArgs = "--user --dir `"$dir`" run"
    $conhost = Join-Path $env:windir 'System32\conhost.exe'
    if ([Environment]::OSVersion.Version.Build -ge 17763 -and (Test-Path $conhost)) {
      $action = New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$exe`" $agentArgs"
    } else {
      $action = New-ScheduledTaskAction -Execute $exe -Argument $agentArgs
    }
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
      -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
      -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $agentTask -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $user) `
      -Settings $settings -Principal $principal `
      -Description 'NexGuard agent: health, and screen time and websites if turned on. Runs as you.' -Force | Out-Null
    if ($env:NEXGUARD_NO_SCHEDULE -ne '1') { Start-ScheduledTask -TaskName $agentTask }

    Write-Host ''
    Write-Host 'Done. The agent runs while you are signed in. You can close this window.' -ForegroundColor Green
    Write-Host "To remove it later, paste:  irm $NgServer/d/uninstall.ps1 | iex"
    Write-Host ''
    return
  }

  # ------------------------------------------ no agent: the health-only check-in task
  # ---------------------------------------------------------------------- ask first
  $preview = Invoke-Pair @{ code = $NgCode; platform = 'windows'; preview = $true }
  Write-Host ''
  Write-Host "  $($preview.disclosure)" -ForegroundColor Cyan
  Write-Host ''
  $answer = Read-Host 'Allow this? Type y and press Enter (or just Enter to cancel)'
  if ($answer -notmatch '^(y|yes)$') {
    Write-Host ''
    Write-Host 'Cancelled. Nothing was changed, and the code still works.'
    return
  }

  # ---------------------------------------------------------------------------- pair
  $paired = Invoke-Pair @{ code = $NgCode; platform = 'windows' }
  if ($paired.token -notmatch '^ngd_[A-Za-z0-9_-]+$') { Stop-Setup 'the dashboard sent an unexpected answer.' }
  $interval = 300
  if ($paired.checkin_seconds) { $interval = [int]$paired.checkin_seconds }

  # -------------------------------------------------------------------------- install
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  @{ server = $NgServer; token = $paired.token; interval = $interval } |
    ConvertTo-Json | Set-Content -Path (Join-Path $dir 'config.json') -Encoding ASCII

  $checkin = @'
# NexGuard check-in: sends CPU, memory, GPU, battery and storage use, the network kind
# (and Wi-Fi name where allowed) and the Windows version to the dashboard. Runs every 5 minutes; reports as often as it asks (interval).
$ErrorActionPreference = 'Stop'
$version = '1.2.0'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $dir 'config.json'
$cfg = Get-Content $configPath -Raw | ConvertFrom-Json
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$lastPath = Join-Path $dir 'last'
$last = 0
if (Test-Path $lastPath) { $last = [int64](Get-Content $lastPath -Raw) }
if ($args -notcontains 'now' -and ($now - $last) -lt ([int64]$cfg.interval - 30)) { return }

$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
$model = (@($cs.Manufacturer, $cs.Model) | Where-Object { $_ -and $_ -notmatch 'To Be Filled' }) -join ' '
$body = @{
  platform    = 'windows'
  os_version  = ('{0} ({1})' -f ($os.Caption -replace '^Microsoft ', ''), $os.Version)
  model       = $model.Trim()
  hostname    = $env:COMPUTERNAME
  app_version = "nexguard-device-ps $version"
}
$battery = Get-CimInstance Win32_Battery | Select-Object -First 1
if ($battery) {
  # BatteryStatus 2 = on mains power; 6-9 = charging.
  $body.battery = @{
    percent  = [int]$battery.EstimatedChargeRemaining
    charging = @(2, 6, 7, 8, 9) -contains [int]$battery.BatteryStatus
  }
}
$drive = New-Object System.IO.DriveInfo ($env:SystemDrive)
$body.storage = @{ total = $drive.TotalSize; free = $drive.AvailableFreeSpace }
# Health. Each reading is optional: a failure leaves it out, never the whole check-in.
try {
  $load = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
  if ($null -ne $load) { $body.cpu = @{ percent = [math]::Round([double]$load, 1) } }
} catch { }
if ($os.TotalVisibleMemorySize) {
  $total = [int64]$os.TotalVisibleMemorySize * 1024
  $body.memory = @{ total = $total; used = $total - [int64]$os.FreePhysicalMemory * 1024 }
}
try {
  # 3D engine use summed over processes, as Task Manager shows it. Counter names are
  # localized, so this is skipped on a Windows that isn't in English.
  $samples = (Get-Counter '\GPU Engine(*engtype_3D)\Utilization Percentage' -ErrorAction Stop).CounterSamples
  $gpu = ($samples | Measure-Object -Property CookedValue -Sum).Sum
  $body.gpu = @{ percent = [math]::Round([math]::Min(100, [double]$gpu), 1) }
} catch { }
try {
  # Network: the adapter carrying the default route (lowest route + interface metric).
  # No default route is "no connection", not an error (Get-NetRoute throws on no match).
  $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
    Sort-Object { [int]$_.RouteMetric + [int]$_.InterfaceMetric } | Select-Object -First 1
  if (-not $route) {
    $body.network = @{ type = 'none'; name = '' }
  } else {
    $adapter = Get-NetAdapter -InterfaceIndex $route.InterfaceIndex -ErrorAction Stop
    # NdisPhysicalMedium: 9 = Wi-Fi, 8 = mobile broadband, 14 = Ethernet. VPNs and
    # virtual switches are left out: their kind says nothing about the connection.
    $medium = [int]$adapter.NdisPhysicalMedium
    if ($medium -eq 9) {
      $ssid = ''
      try {
        # Windows 11 24H2+ hides the name from apps without location access; then the
        # output has no SSID line and only the kind is sent.
        $line = netsh wlan show interfaces 2>$null | Where-Object { $_ -match '^\s*SSID\s*:' } |
          Select-Object -First 1
        if ($line) { $ssid = ($line -replace '^\s*SSID\s*:\s*', '').Trim() }
      } catch { }
      $body.network = @{ type = 'wifi'; name = $ssid }
    } elseif ($medium -eq 8) {
      $body.network = @{ type = 'cellular'; name = '' }
    } elseif ($medium -eq 14 -and -not $adapter.Virtual) {
      $body.network = @{ type = 'ethernet'; name = '' }
    }
  }
} catch { }

$errorPath = Join-Path $dir 'error'
try {
  $answer = Invoke-RestMethod -Method Post -Uri "$($cfg.server)/device/v1/checkin" -TimeoutSec 20 `
    -Headers @{ Authorization = "Bearer $($cfg.token)" } -UserAgent "nexguard-device-ps/$version" `
    -ContentType 'application/json; charset=utf-8' `
    -Body ([Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Compress)))
  Set-Content -Path $lastPath -Value $now -Encoding ASCII
  Remove-Item $errorPath -ErrorAction SilentlyContinue
  if ($answer.checkin_seconds -and [int]$answer.checkin_seconds -ne [int]$cfg.interval) {
    $cfg.interval = [int]$answer.checkin_seconds
    $cfg | ConvertTo-Json | Set-Content -Path $configPath -Encoding ASCII
  }
} catch {
  $status = 0
  if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
  if ($status -eq 401) {
    # Removed on the dashboard (or paired again elsewhere): stop for good, tidy up.
    & (Join-Path $dir 'uninstall.ps1') -Quiet
    return
  }
  Add-Content -Path $errorPath -Value ('{0} HTTP {1} {2}' -f (Get-Date -Format s), $status, $_.Exception.Message)
}
'@
  Set-Content -Path (Join-Path $dir 'checkin.ps1') -Value $checkin -Encoding ASCII

  Set-Content -Path (Join-Path $dir 'uninstall.ps1') -Value $uninstall -Encoding ASCII

  # ------------------------------------------------------------- run every 5 minutes
  $script = Join-Path $dir 'checkin.ps1'
  $psArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`""
  # conhost --headless runs it with no window at all (Windows 10 1809 and later);
  # otherwise a hidden PowerShell window, which may flash for a moment.
  $conhost = Join-Path $env:windir 'System32\conhost.exe'
  if ([Environment]::OSVersion.Version.Build -ge 17763 -and (Test-Path $conhost)) {
    $action = New-ScheduledTaskAction -Execute $conhost -Argument "--headless powershell.exe $psArgs"
  } else {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-WindowStyle Hidden $psArgs"
  }
  $user = "$env:USERDOMAIN\$env:USERNAME"
  $triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $user),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
      -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650))
  )
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
  $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings `
    -Principal $principal -Description 'Reports CPU, memory, GPU, battery, storage, network and Windows version to NexGuard.' -Force | Out-Null

  # -------------------------------------------------------------------- first check-in
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script now
  Write-Host ''
  if (Test-Path (Join-Path $dir 'error')) {
    Write-Host "Paired as `"$($paired.device.name)`", but the first check-in failed. It will try again every 5 minutes." -ForegroundColor Yellow
  } else {
    Write-Host "Done. This computer is connected as `"$($paired.device.name)`" in $($paired.device.account)." -ForegroundColor Green
    Write-Host 'It reports every 5 minutes while you are signed in. You can close this window.'
  }
  Write-Host "To remove it later, paste:  irm $NgServer/d/uninstall.ps1 | iex"
  Write-Host ''
}
{% endverbatim %}
