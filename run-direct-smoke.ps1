# Direct (non-Jev) ACT-LAYER smoke test, deterministic: fresh Untitled -> type -> save as E:\tmp\n.txt.
$ErrorActionPreference = "Continue"
$proj = "E:\Projects\jev-computer-use"

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class M {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
}
"@

# Open a fresh Notepad. Win11 session-restore may bring back previous unsaved
# windows (titles like "*Hello - Notepad") instead of "*Untitled*", so target the
# NEWEST unsaved window (any title not ending in "- Notepad" already-saved, and
# excluding the user's real document by pid list below).
$protectedPids = @(3168)
Start-Process notepad
Start-Sleep -Seconds 2
$np = Get-Process notepad | Where-Object { $protectedPids -notcontains $_.Id -and $_.MainWindowTitle -ne "" } | Sort-Object StartTime -Descending | Select-Object -First 1
if (-not $np) {
    Write-Output "ERROR: no notepad window to target"
    exit 1
}
$hwnd = $np.MainWindowHandle
Write-Output ("target window: " + $np.MainWindowTitle + " hwnd=" + $hwnd)
[M]::SetForegroundWindow($hwnd) | Out-Null
[M]::ShowWindow($hwnd, 3) | Out-Null
Start-Sleep -Seconds 1
$fg = [M]::GetForegroundWindow()
Write-Output "foreground: hwnd=$fg (need $hwnd)"

& "$proj\.venv\Scripts\python.exe" -c @"
import pyautogui, time
pyautogui.click(960, 480); time.sleep(0.4)
pyautogui.typewrite('Hello', interval=0.05); time.sleep(0.4)
pyautogui.hotkey('ctrl', 's'); time.sleep(1.8)
pyautogui.typewrite('n.txt', interval=0.03); time.sleep(0.4)  # NAME only; a full path here = invalid-name error
pyautogui.hotkey('alt', 's'); time.sleep(1.5)
print('direct actions done')
"@

Start-Sleep -Seconds 2
# The dialog saves into whatever folder it last remembered (per-app, via ComDlg32),
# so verify via the shell's Recent-items link for n.txt - the ground truth for where
# it actually landed.
$candidates = @("E:\tmp\n.txt", "$env:USERPROFILE\Documents\n.txt", "$env:USERPROFILE\Desktop\n.txt")
$lnk = "$env:APPDATA\Microsoft\Windows\Recent\n.txt.lnk"
if (Test-Path -LiteralPath $lnk) {
    $sh = New-Object -ComObject WScript.Shell
    $candidates += $sh.CreateShortcut($lnk).TargetPath
}
$found = $candidates | Where-Object { Test-Path $_ } | Select-Object -Unique
if ($found) {
    Write-Output ("FILE CREATED: " + ($found -join ", "))
    Write-Output ("content: " + (Get-Content $found[0] -Raw))
} else {
    Write-Output "FILE NOT FOUND"
    if (Test-Path -LiteralPath $lnk) { Write-Output ("last saved at: " + $sh.CreateShortcut($lnk).TargetPath) }
}