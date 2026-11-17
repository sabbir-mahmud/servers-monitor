{% verbatim %}# Stop NexGuard check-ins on this Windows computer and remove everything it installed.
# The dashboard still lists the computer until someone removes it there.
& {
  Unregister-ScheduledTask -TaskName 'NexGuard device check-in' -Confirm:$false -ErrorAction SilentlyContinue
  $dir = Join-Path $env:LOCALAPPDATA 'NexGuard'
  if (Test-Path $dir) {
    Remove-Item -Recurse -Force $dir
    Write-Host 'NexGuard is removed from this computer.'
  } else {
    Write-Host "NexGuard wasn't installed for this user."
  }
}
{% endverbatim %}
