#!/usr/bin/env python3
"""Validated Win11 Save-As recipe (2026-09-21).

Demonstrates the deterministic act-layer flow that lands a file in a chosen folder
using ONLY OCR-driven clicks (the same primitives the Jev loop uses):

  1. type "Hello", Ctrl+S
  2. click the E: drive node in the left tree
  3. click the file list, type folder name ("tmp") to filter, double-click the row
  4. click the File name field, Ctrl+A, type the NAME only ("n.txt") - a full path
     here triggers Windows "file name not valid (special characters)"
  5. click the real Save button (exact text "Save" with y > 500 - the dialog title
     "Save as" also OCRs at y ~ 20 and must be ignored)
  6. verify E:\\tmp\\n.txt exists with content "Hello"

Safe to re-run: it launches its own Notepad window and targets the newest Untitled,
never touching other documents.
"""
import sys, time, os, ctypes, subprocess
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import pyautogui
from rapidocr_onnxruntime import RapidOCR

OCR = RapidOCR()
SHOT = r"C:\Users\USER\AppData\Local\Temp\opencode\path3"
os.makedirs(SHOT, exist_ok=True)
user32 = ctypes.windll.user32

def shot(name):
    p = os.path.join(SHOT, name + ".png"); pyautogui.screenshot(p); return p

def ocr(p, label):
    print("==", label, "==")
    res, _ = OCR(p)
    items = []
    for box, text, score in (res or []):
        x = sum(pt[0] for pt in box) / 4; y = sum(pt[1] for pt in box) / 4
        items.append((int(x), int(y), text))
        print(f"({int(x):4},{int(y):4}) {text!r}")
    return items

def find(items, *subs):
    for x, y, t in items:
        tl = t.lower()
        if all(s.lower() in tl for s in subs):
            return x, y
    return None

def list_notepad():
    out = []
    def cb(h, _):
        if user32.IsWindowVisible(h):
            n = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(h, n, 512)
            t = n.value
            if t and "Notepad" in t: out.append((h, t))
        return True
    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(cb), 0)
    return out

# fresh blank window
subprocess.Popen(["notepad.exe"]); time.sleep(2.2)
pyautogui.hotkey("ctrl", "shift", "n"); time.sleep(1.2)
wins = list_notepad()
print("windows:", [(hex(h), t) for h, t in wins])
target = max((h for h, t in wins if "Untitled" in t), default=None)
if not target:
    print("NO Untitled"); sys.exit(1)
pyautogui.keyDown("alt"); time.sleep(0.05); pyautogui.keyUp("alt"); time.sleep(0.15)
user32.ShowWindow(target, 3); user32.SetForegroundWindow(target); time.sleep(0.5)

pyautogui.click(960, 480); time.sleep(0.3)
pyautogui.typewrite("Hello", interval=0.05); time.sleep(0.3)
pyautogui.hotkey("ctrl", "s"); time.sleep(1.6)
items = ocr(shot("1_dialog"), "dialog list")

# click the E: tree node ("Nouveau nom (E:)")
e_node = find(items, "(e:)") or find(items, "nouveau nom")
if not e_node:
    print("E: node not found; abort"); sys.exit(2)
print("click E node at", e_node)
pyautogui.click(*e_node); time.sleep(1.0)
items = ocr(shot("2_after_e"), "after E click")

# click the file list then type tmp to filter/select it
pyautogui.click(500, 300); time.sleep(0.3)
pyautogui.typewrite("tmp", interval=0.06); time.sleep(0.6)
items = ocr(shot("2b_tmp_filter"), "after typing tmp")
tmp = find(items, "tmp")
if not tmp:
    print("tmp not visible after filter"); sys.exit(3)
print("double-click tmp at", tmp)
pyautogui.doubleClick(*tmp); time.sleep(1.0)
items = ocr(shot("3_after_tmp"), "after tmp click")

# breadcrumb should show E:\tmp now; click into File name field and type
namef = find(items, "file name")
if not namef:
    print("file name field not found"); sys.exit(4)
fx, fy = namef; fx += 130
print("click filename field at", (fx, fy))
pyautogui.click(fx, fy); time.sleep(0.3)
pyautogui.hotkey("ctrl", "a"); time.sleep(0.1)
pyautogui.typewrite("n.txt", interval=0.04); time.sleep(0.3)
items = ocr(shot("4_named"), "name typed")

save = find(items, "save")
if save and save[1] < 500:
    save = None
    for x, y, t in items:
        if t.strip().lower() == "save" and y > 500:
            save = (x, y); break
if not save:
    print("Save button not found"); sys.exit(5)
print("click Save at", save)
pyautogui.click(*save); time.sleep(1.6)
items = ocr(shot("5_done"), "after save")

p = r"E:\tmp\n.txt"
print("FINAL:", p, "exists=", os.path.exists(p))
if os.path.exists(p):
    print("content:", open(p, encoding="utf-8").read())