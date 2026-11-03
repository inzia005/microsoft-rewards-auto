r"""Register / remove / inspect the Windows scheduled task for the runner.

Usage:  python schedule.py install | remove | status
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TASK = "MicrosoftRewardsAuto"
BASE = Path(__file__).resolve().parent
SCRIPT = BASE / "run.py"
# pythonw has no console; keep child PowerShell from allocating a visible one.
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
# Prefer the windowless interpreter from the same environment (no console flash).
_pyw = Path(sys.executable).with_name("pythonw.exe")
PYW = _pyw if _pyw.is_file() else Path(sys.executable)

INSTALL = rf'''
$ErrorActionPreference = "Stop"
$action = New-ScheduledTaskAction -Execute "{PYW}" -Argument '-X utf8 "{SCRIPT}" --auto'
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 30)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "{TASK}" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force `
    -Description "Finish Microsoft Rewards daily tasks when the PC is idle." | Out-Null
Write-Output "installed"
'''

REMOVE = rf'''
$ErrorActionPreference = "Stop"
Unregister-ScheduledTask -TaskName "{TASK}" -Confirm:$false
Write-Output "removed"
'''

STATUS = rf'''
$t = Get-ScheduledTask -TaskName "{TASK}" -ErrorAction SilentlyContinue
if ($null -eq $t) {{ Write-Output "not-installed" }} else {{
  $i = $t | Get-ScheduledTaskInfo
  Write-Output ("state    : " + $t.State)
  Write-Output ("last run : " + $i.LastRunTime)
  Write-Output ("last code: " + $i.LastTaskResult)
  Write-Output ("next run : " + $i.NextRunTime)
}}
'''


def run_ps(script: str) -> int:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=120, creationflags=NOWIN,
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if out:
        print(out)
    if proc.returncode != 0 and err:
        print(err, file=sys.stderr)
    return proc.returncode


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "install":
        return run_ps(INSTALL)
    if action == "remove":
        return run_ps(REMOVE)
    if action == "status":
        return run_ps(STATUS)
    print("usage: python schedule.py install|remove|status")
    return 1


if __name__ == "__main__":
    sys.exit(main())
