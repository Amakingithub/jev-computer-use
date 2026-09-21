# Direct (non-Jev) ACT-LAYER smoke test, deterministic: fresh blank Notepad window
# -> type Hello -> Save As n.txt (name only, no folder steering) -> verify a file
# named n.txt appears (dialog saves into whatever folder it last remembered).
$ErrorActionPreference = "Continue"
$proj = "E:\Projects\jev-computer-use"

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class M {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
}
"@

function Get-NotepadWindows {
    $npids = @(Get-Process notepad -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    $res = New-Object System.Collections.Generic.List[System.Object]
    [M]::EnumWindows({
        param($h, $l)
        $sb = New-Object System.Text.StringBuilder 256
        [void][M]::GetWindowText($h, $sb, 256)
        $pid2 = [uint32]0
        [void][M]::GetWindowThreadProcessId($h, [ref]$pid2)
        if ([M]::IsWindowVisible($h) -and $npids -contains [int]$pid2) {
            $res.Add([PSCustomObject]@{ Hwnd=$h; Title=$sb.ToString() })
        }
        return $true
    }, [IntPtr]::Zero) | Out-Null
    return $res
}

$protectedPids = @(3168)

# 1) launch notepad, 2) Ctrl+N to open a FRESH UNSAVED TAB (session-restore may have
#    resurrected old windows), 3) pick an "Untitled" top-level window via EnumWindows.
Start-Process notepad
Start-Sleep -Seconds 3
& "$proj\.venv\Scripts\python.exe" -c "import pyautogui,time; pyautogui.hotkey('ctrl','n'); time.sleep(1.0)"
Start-Sleep -Seconds 1

$wins = Get-NotepadWindows | ForEach-Object { $_ }
Write-Output ("windows: " + (($wins | ForEach-Object { "'" + $_.Title + "' (" + $_.Hwnd + ")" }) -join ", "))
$target = $wins | Where-Object { $_.Title -like "Untitled*" } | Select-Object -First 1
if (-not $target) {
    Write-Output "ERROR: no Untitled notepad window found"
    exit 1
}
$hwnd = $target.Hwnd
Write-Output ("target window: '" + $target.Title + "' hwnd=" + $hwnd)
& "$proj\.venv\Scripts\python.exe" -c "import pyautogui,time; pyautogui.keyDown('alt'); time.sleep(0.05); pyautogui.keyUp('alt'); time.sleep(0.15)"
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
# it landed.
$candidates = @("E:\tmp\n.txt", "$env:USERPROFILE\Documents\n.txt", "$env:USERPROFILE\Desktop\n.txt")
$lnk = "$env:APPDATA\Microsoft\Windows\Recent\n.txt.lnk"
if (Test-Path -LiteralPath $lnk) {
    $sh = New-Object -ComObject WScript.Shell
    $candidates += $sh.CreateShortcut($lnk).TargetPath
}
$found = @($candidates) | Where-Object { Test-Path $_ } | Select-Object -Unique
if ($found) {
    Write-Output ("FILE CREATED: " + ($found -join ", "))
    Write-Output ("content: " + (Get-Content ([string]$found[0]) -Raw))
} else {
    Write-Output "FILE NOT FOUND"
    if (Test-Path -LiteralPath $lnk) { Write-Output ("last saved at: " + $sh.CreateShortcut($lnk).TargetPath) }
    else { Write-Output "no Recent lnk for n.txt" }
}