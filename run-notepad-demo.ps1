# Supervised Notepad demo for jev-computer-use.
# Uses a FRESH "Untitled - Notepad" window (existing documents are never touched),
# forces it to the FOREGROUND via Alt-trick + SetForegroundWindow (bypasses the
# Windows foreground lock for a background process), verifies it is foreground,
# then runs the agent with --approval none (demo mode). Every step still passes
# the Jev risk gate + dHash verification.
#
#   powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File run-notepad-demo.ps1

$ErrorActionPreference = "Continue"
$proj = "E:\Projects\jev-computer-use"

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class Win32 {
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
    [Win32]::EnumWindows({
        param($h, $l)
        $sb = New-Object System.Text.StringBuilder 256
        [void][Win32]::GetWindowText($h, $sb, 256)
        $pid2 = [uint32]0
        [void][Win32]::GetWindowThreadProcessId($h, [ref]$pid2)
        if ([Win32]::IsWindowVisible($h) -and $npids -contains [int]$pid2) {
            $res.Add([PSCustomObject]@{ Hwnd=$h; Title=$sb.ToString() })
        }
        return $true
    }, [IntPtr]::Zero) | Out-Null
    return $res
}

$wins = Get-NotepadWindows
$blankRe = "^(Untitled|Sans titre)"
$target = $wins | Where-Object { $_.Title -match $blankRe } | Select-Object -First 1
if (-not $target) {
    Write-Output "no blank window; opening a fresh Notepad"
    Start-Process notepad
    Start-Sleep -Seconds 2
    $target = (Get-NotepadWindows) | Where-Object { $_.Title -match $blankRe } | Select-Object -First 1
}

if (-not $target) {
    Write-Output "ERROR: no blank Notepad window found (titles seen: $((($wins + (Get-NotepadWindows)) | ForEach-Object { $_.Title }) -join ', '))"
    exit 1
}

Write-Output ("target: '" + $target.Title + "' hwnd=" + $target.Hwnd)
Write-Output ("existing documents untouched: " + ((Get-NotepadWindows | Where-Object { $_.Title -notmatch $blankRe } | ForEach-Object { $_.Title }) -join ", "))

# Content is delivered OUT-OF-BAND (Jev only decides; it never produces free text):
# the first keystrokes are "Hello", the save dialog is fed from the clipboard.
# VALIDATED 2026-09-21: the "File name" box takes a NAME only - a full path
# (E:\tmp\n.txt) triggers Windows "file name is not valid ... special characters"
# because of the backslashes. Steering the dialog to a specific folder is a
# deterministic act-layer recipe (click E: tree node -> type "tmp" to filter ->
# double-click -> type name -> click Save button below y=500), NOT a Jev decision.
# The Jev goal below therefore asks for a plain save-as filename.
Set-Clipboard -Value "n.txt"
& "$proj\.venv\Scripts\python.exe" -c "import pyautogui,time; pyautogui.keyDown('alt'); time.sleep(0.05); pyautogui.keyUp('alt'); time.sleep(0.15)"

# Alt-trick: a real Alt keydown/up grants the calling process foreground rights,
# so SetForegroundWindow works from a background (hidden) process.
$py = "$proj\.venv\Scripts\python.exe"
& $py -c "import pyautogui,time; pyautogui.keyDown('alt'); time.sleep(0.05); pyautogui.keyUp('alt'); time.sleep(0.15)"
Start-Sleep -Milliseconds 200
[void][Win32]::SetForegroundWindow($target.Hwnd)
[void][Win32]::ShowWindow($target.Hwnd, 3)  # SW_MAXIMIZE so OCR sees pure Notepad chrome
Start-Sleep -Seconds 1

# Anchor the caret inside the editor (maximized Notepad center) so keystrokes land.
& $py -c "import pyautogui,time; pyautogui.click(960, 480); time.sleep(0.3)"

$fg = [Win32]::GetForegroundWindow()
$sb2 = New-Object System.Text.StringBuilder 256
[void][Win32]::GetWindowText($fg, $sb2, 256)
Write-Output "foreground NOW: '$($sb2.ToString())' (hwnd=$fg)"
if ($fg -ne $target.Hwnd) {
    Write-Output "WARN: foreground lock not bypassed; OCR may see another window"
}

# Recording: annotated PNG per step + a stitched GIF, so this demo doubles as the
# --record/replay validation (see the replay line after the run).
$rec = "$proj\demo-record-$(Get-Date -Format yyyyMMdd-HHmmss)"
& "$proj\.venv\Scripts\python.exe" -m jev_computer_use.computer_use.run_agent `
    --goal "in the foreground Notepad window, type Hello, then save the file (Save As, filename n.txt)" `
    --approval none `
    --input-text "Hello" `
    --press-key "ctrl+s|ctrl+v|enter" `
    --show-guide `
    --record $rec `
    --max-steps 12

Write-Output "exit code: $LASTEXITCODE"
if (Test-Path "$rec\step_*.png") {
    & "$proj\.venv\Scripts\python.exe" -m jev_computer_use.computer_use.run_agent replay $rec
} else {
    Write-Output "recording empty (no frame captured) at $rec"
}