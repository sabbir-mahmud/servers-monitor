{% autoescape off %}# {{ site_name }}: connect this Windows computer.
#
# Downloaded from {{ server }} for one pairing code. It:
#   1. shows who will see this computer, and pairs only if you say yes;
#   2. installs the NexGuard agent from that dashboard as a protected service (Windows
#      asks an administrator to allow it): health (CPU, memory, GPU, battery, storage,
#      network, Windows version), and screen time, websites and website blocking if the
#      dashboard turns them on. Without an administrator, the same agent running as you
#      (no blocking);
#   3. checks in once so the dashboard shows the computer straight away.
#
# The agent pairs and checks in through the dashboard's Redis relay ($NgRelay below: a
# login that can only send pairing requests), never to the dashboard itself.
#
# To remove it later:  irm {{ server }}/d/uninstall.ps1 | iex
$NgServer = '{{ server }}'
$NgCode = '{{ code }}'
$NgRelay = '{{ relay }}'
{% endautoescape %}{% verbatim %}
& {
  $ErrorActionPreference = 'Stop'
  # Windows PowerShell 5.1 on older builds still defaults to TLS 1.0.
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

  $dir = Join-Path $env:LOCALAPPDATA 'NexGuard'

  function Stop-Setup([string]$message) {
    Write-Host ''
    Write-Host "Couldn't connect this computer: $message" -ForegroundColor Red
    Write-Host ''
    throw 'NexGuard setup stopped.'
  }

  if (-not $NgRelay) { Stop-Setup "the dashboard can't take computers right now (its Redis relay isn't reachable). Try again later." }

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
      else { Write-Host "(The agent download didn't match its checksum.)" }
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
& '$exeQ' pair '$NgRelay' '$NgCode'
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
    $pairArgs += @($NgRelay, $NgCode)
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

  Stop-Setup "this dashboard has no NexGuard agent for this computer. Ask whoever runs it to build the agent (make device-agent)."
}
{% endverbatim %}
