{% verbatim %}# Stop NexGuard on this Windows computer (the agent or the check-in task) and remove
# everything it installed. The dashboard still lists the computer until someone removes it.
& {
  $dir = Join-Path $env:LOCALAPPDATA 'NexGuard'
  foreach ($task in 'NexGuard device check-in', 'NexGuard agent') {
    Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
  }
  $exe = Join-Path $dir 'nexguard.exe'
  Get-Process -Name nexguard -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe } |
    Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 500  # let Windows release the program file
  if (Test-Path $dir) {
    Remove-Item -Recurse -Force $dir
    Write-Host 'NexGuard is removed from this computer.'
  } else {
    Write-Host "NexGuard wasn't installed for this user."
  }
}
{% endverbatim %}