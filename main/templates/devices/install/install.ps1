{% autoescape off %}# {{ site_name }}: connect this Windows computer.
#
# Downloaded from {{ server }} for one pairing code. It:
#   1. shows who will see this computer, and pairs only if you say yes;
#   2. adds a scheduled task that checks in as you every 5 minutes
#      (battery, storage, Windows version; no administrator rights needed);
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
# NexGuard check-in: sends battery, storage and the Windows version to the dashboard.
# Runs every 5 minutes; actually reports as often as the dashboard asks (interval).
$ErrorActionPreference = 'Stop'
$version = '1.0.0'
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

  $uninstall = @'
# Stop NexGuard check-ins on this computer and remove everything it installed.
param([switch]$Quiet)
Unregister-ScheduledTask -TaskName 'NexGuard device check-in' -Confirm:$false -ErrorAction SilentlyContinue
$dir = Join-Path $env:LOCALAPPDATA 'NexGuard'
Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue
if (-not $Quiet) { Write-Host 'NexGuard is removed from this computer.' }
'@
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
    -Principal $principal -Description 'Reports battery, storage and Windows version to NexGuard.' -Force | Out-Null

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
