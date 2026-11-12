"""Tkinter GUI."""
import base64
import ctypes
import json
import queue
import re
import shutil
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

import cv2

from . import gamewin
from .bot import Bot
from .matcher import TemplateStore

FONT = "Leelawadee UI"   # ships with Windows, has proper Thai glyphs
VK_F8 = 0x77
WM_HOTKEY = 0x0312

DEFAULTS = {
    "process": "fifazf",
    "max_rounds": 10,
    "unlimited": False,
    "click_mode": "background",
    "interval": 0.6,
    "after_click": 1.0,
    "stuck_seconds": 300,
}
FIRST_PROFILE = "จำลองลีกกับบอท"
MODES = {"เบื้องหลัง (ไม่ขยับเมาส์)": "background", "คลิกจริง (ขยับเมาส์)": "foreground"}


def hotkey_loop(callback):
    """Global F8. Runs in its own thread with its own message loop."""
    user32 = ctypes.windll.user32
    if not user32.RegisterHotKey(None, 1, 0, VK_F8):
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            callback()


def to_photo(bgr):
    ok, buf = cv2.imencode(".png", bgr)
    return tk.PhotoImage(data=base64.b64encode(buf.tobytes()))


class CaptureDialog(tk.Toplevel):
    """Show a game screenshot; the user drags a box around a button."""

    def __init__(self, app, frame):
        super().__init__(app.root)
        self.app = app
        self.frame = frame
        self.title("ลากกรอบรอบปุ่มที่ต้องการให้กด")
        self.transient(app.root)
        fh, fw = frame.shape[:2]
        self.scale = min(1.0, (self.winfo_screenwidth() - 120) / fw,
                         (self.winfo_screenheight() - 260) / fh)
        shown = frame if self.scale == 1 else cv2.resize(
            frame, (int(fw * self.scale), int(fh * self.scale)), interpolation=cv2.INTER_AREA)
        self.photo = to_photo(shown)
        self.canvas = tk.Canvas(self, width=shown.shape[1], height=shown.shape[0],
                                cursor="crosshair", highlightthickness=0)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.pack()
        self.box = None
        self.rect = None
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)

        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Label(bar, text="ชื่อปุ่ม").pack(side="left")
        self.name = tk.StringVar()
        ttk.Entry(bar, textvariable=self.name, width=24).pack(side="left", padx=6)
        self.count = tk.BooleanVar()
        ttk.Checkbutton(bar, text="กดปุ่มนี้ = นับ 1 รอบ", variable=self.count).pack(side="left", padx=6)
        ttk.Button(bar, text="บันทึก", command=self.on_save).pack(side="right")
        ttk.Button(bar, text="ยกเลิก", command=self.destroy).pack(side="right", padx=6)

        bar2 = ttk.Frame(self, padding=(8, 0, 8, 8))
        bar2.pack(fill="x")
        self.stop = tk.BooleanVar()
        ttk.Checkbutton(bar2, text="เจอภาพนี้ให้หยุดบอท (ไม่กด)", variable=self.stop).pack(side="left")
        self.local = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar2, text="หาเฉพาะตำแหน่งนี้ (แยกปุ่มหน้าตาเหมือนกัน)",
                        variable=self.local).pack(side="left", padx=12)

    def on_press(self, e):
        self.box = [e.x, e.y, e.x, e.y]
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(*self.box, outline="#ffd21a", width=2)

    def on_drag(self, e):
        self.box[2:] = [e.x, e.y]
        self.canvas.coords(self.rect, *self.box)

    def on_save(self):
        name = self.name.get().strip()
        if not name or not self.box:
            messagebox.showwarning("ยังไม่ครบ", "ลากกรอบและตั้งชื่อปุ่มก่อน", parent=self)
            return
        fh, fw = self.frame.shape[:2]
        x0, x1 = sorted(int(v / self.scale) for v in (self.box[0], self.box[2]))
        y0, y1 = sorted(int(v / self.scale) for v in (self.box[1], self.box[3]))
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(fw, x1), min(fh, y1)
        if x1 - x0 < 8 or y1 - y0 < 8:
            messagebox.showwarning("กรอบเล็กไป", "ลากกรอบให้ใหญ่กว่านี้", parent=self)
            return
        try:
            stop = self.stop.get()
            self.app.store.add(name, self.frame[y0:y1, x0:x1].copy(), fh,
                               count_round=self.count.get() and not stop,
                               action="stop" if stop else "click",
                               box=[x0, y0, x1, y1], base_w=fw, local=self.local.get())
        except ValueError:
            messagebox.showwarning("ชื่อซ้ำ", "มีปุ่มชื่อนี้แล้ว", parent=self)
            return
        self.app.refresh_list()
        self.destroy()


class App:
    def __init__(self, root, base_dir):
        self.root = root
        self.base = Path(base_dir)
        self.settings_path = self.base / "settings.json"
        self.settings = dict(DEFAULTS)
        if self.settings_path.exists():
            try:
                self.settings.update(json.loads(self.settings_path.read_text(encoding="utf-8")))
            except ValueError:
                pass
        self.profiles_dir = self.base / "profiles"
        self.init_profiles()
        self.store = TemplateStore(self.profiles_dir / self.settings["profile"])
        self.bot = None
        self.q = queue.Queue()
        self.build()
        self.refresh_list()
        threading.Thread(target=hotkey_loop, args=(lambda: self.q.put(("hotkey", None)),),
                         daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll()
        self.watch_game()

    # ---------- profiles (one set of buttons per game mode) ----------
    def profile_names(self):
        return sorted(p.name for p in self.profiles_dir.iterdir() if p.is_dir())

    def init_profiles(self):
        self.profiles_dir.mkdir(exist_ok=True)
        old = self.base / "templates"   # layout before profiles existed
        if (old / "index.json").exists() and not self.profile_names():
            old.rename(self.profiles_dir / FIRST_PROFILE)
        if not self.profile_names():
            (self.profiles_dir / FIRST_PROFILE).mkdir()
        if self.settings.get("profile") not in self.profile_names():
            self.settings["profile"] = self.profile_names()[0]

    def running(self):
        return bool(self.bot and self.bot.is_alive())

    def load_profile(self, name):
        self.settings["profile"] = name
        self.store = TemplateStore(self.profiles_dir / name)
        self.profile.set(name)
        self.profile_box.configure(values=self.profile_names())
        self.refresh_list()
        self.save_settings()

    def on_profile_selected(self, _event=None):
        if self.running():
            self.profile.set(self.settings["profile"])
            messagebox.showinfo("กำลังทำงาน", "หยุดบอทก่อนเปลี่ยนโหมด")
            return
        self.load_profile(self.profile.get())

    def ask_profile_name(self, title, initial=""):
        name = simpledialog.askstring(title, "ชื่อโหมด", parent=self.root, initialvalue=initial)
        name = (name or "").strip().rstrip(".")
        if not name:
            return None
        if re.search(r'[\\/:*?"<>|]', name):
            messagebox.showwarning("ชื่อใช้ไม่ได้", 'ห้ามมีอักขระ \\ / : * ? " < > |')
            return None
        if name.lower() in (n.lower() for n in self.profile_names()):
            messagebox.showwarning("ชื่อซ้ำ", "มีโหมดชื่อนี้แล้ว")
            return None
        return name

    def new_profile(self):
        if self.running():
            messagebox.showinfo("กำลังทำงาน", "หยุดบอทก่อน")
            return
        name = self.ask_profile_name("โหมดใหม่")
        if name:
            (self.profiles_dir / name).mkdir()
            self.load_profile(name)

    def rename_profile(self):
        if self.running():
            messagebox.showinfo("กำลังทำงาน", "หยุดบอทก่อน")
            return
        old = self.settings["profile"]
        name = self.ask_profile_name("เปลี่ยนชื่อโหมด", old)
        if name:
            (self.profiles_dir / old).rename(self.profiles_dir / name)
            self.load_profile(name)

    def delete_profile(self):
        if self.running():
            messagebox.showinfo("กำลังทำงาน", "หยุดบอทก่อน")
            return
        name = self.settings["profile"]
        count = len(self.store.items)
        if not messagebox.askyesno("ลบโหมด", f"ลบโหมด “{name}” พร้อมปุ่ม {count} ปุ่ม?\nกู้คืนไม่ได้"):
            return
        shutil.rmtree(self.profiles_dir / name)
        if not self.profile_names():
            (self.profiles_dir / FIRST_PROFILE).mkdir()
        self.load_profile(self.profile_names()[0])

    # ---------- layout ----------
    def build(self):
        root = self.root
        root.title("FO3 Auto Clicker")
        root.option_add("*Font", (FONT, 10))
        style = ttk.Style(root)
        style.configure(".", font=(FONT, 10))
        style.configure("Treeview.Heading", font=(FONT, 10, "bold"))
        style.configure("Big.TButton", font=(FONT, 13, "bold"), padding=10)
        pad = {"padx": 12, "pady": 6}

        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        self.game_label = ttk.Label(top, text="กำลังหาเกม…")
        self.game_label.pack(side="left")
        if gamewin.is_admin():
            ttk.Label(top, text="Admin", foreground="#1a8f4c").pack(side="right")
        else:
            ttk.Button(top, text="เปิดใหม่แบบ Admin", command=self.relaunch_admin).pack(side="right")

        prow = ttk.Frame(root)
        prow.pack(fill="x", **pad)
        ttk.Label(prow, text="โหมด").pack(side="left")
        self.profile = tk.StringVar(value=self.settings["profile"])
        self.profile_box = ttk.Combobox(prow, textvariable=self.profile, state="readonly",
                                        values=self.profile_names(), width=24)
        self.profile_box.pack(side="left", padx=6)
        self.profile_box.bind("<<ComboboxSelected>>", self.on_profile_selected)
        for text, cmd in (("+ ใหม่", self.new_profile), ("เปลี่ยนชื่อ", self.rename_profile),
                          ("ลบ", self.delete_profile)):
            ttk.Button(prow, text=text, command=cmd, width=len(text) + 2).pack(side="left", padx=2)

        self.round_label = ttk.Label(root, text="0", font=(FONT, 30, "bold"))
        self.round_label.pack(**pad)
        self.status = ttk.Label(root, text="พร้อมใช้งาน")
        self.status.pack()

        opts = ttk.Frame(root)
        opts.pack(fill="x", **pad)
        ttk.Label(opts, text="จำนวนรอบ").grid(row=0, column=0, sticky="w")
        self.max_rounds = tk.IntVar(value=self.settings["max_rounds"])
        ttk.Spinbox(opts, from_=1, to=9999, width=6, textvariable=self.max_rounds).grid(row=0, column=1, padx=6)
        self.unlimited = tk.BooleanVar(value=self.settings["unlimited"])
        ttk.Checkbutton(opts, text="ไม่จำกัด", variable=self.unlimited).grid(row=0, column=2)
        ttk.Label(opts, text="วิธีคลิก").grid(row=1, column=0, sticky="w", pady=(6, 0))
        label = next(k for k, v in MODES.items() if v == self.settings["click_mode"])
        self.mode = tk.StringVar(value=label)
        ttk.Combobox(opts, textvariable=self.mode, values=list(MODES), state="readonly",
                     width=24).grid(row=1, column=1, columnspan=2, padx=6, pady=(6, 0), sticky="w")

        self.hide = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="ซ่อนหน้าต่างเกม (ทำงานต่อได้ระหว่างใช้โปรแกรมอื่น)",
                        variable=self.hide, command=self.apply_hide).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        box = ttk.LabelFrame(root, text="ปุ่มที่ให้กด (บนสุด = สำคัญสุด)")
        box.pack(fill="both", expand=True, **pad)
        cols = ("name", "kind", "thr", "local", "round", "on")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", height=7, selectmode="browse")
        for col, text, width in (("name", "ชื่อปุ่ม", 170), ("kind", "เมื่อเจอ", 70),
                                 ("thr", "ความแม่น", 70), ("local", "ล็อกตำแหน่ง", 90),
                                 ("round", "นับรอบ", 60), ("on", "เปิดใช้", 60)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w" if col == "name" else "center")
        self.tree.pack(fill="both", expand=True, padx=6, pady=6)
        btns = ttk.Frame(box)
        btns.pack(fill="x", padx=6, pady=(0, 6))
        for text, cmd in (("+ เพิ่มจากจอเกม", self.add_template), ("▲", lambda: self.move(-1)),
                          ("▼", lambda: self.move(1)), ("เปิด/ปิด", self.toggle_enabled),
                          ("นับรอบ", self.toggle_round), ("ความแม่น", self.edit_threshold),
                          ("ทดสอบ", self.test_match), ("ลบ", self.delete)):
            ttk.Button(btns, text=text, command=cmd, width=len(text) + 2).pack(side="left", padx=2)
        btns2 = ttk.Frame(box)
        btns2.pack(fill="x", padx=6, pady=(0, 6))
        for text, cmd in (("สลับ กด / หยุดบอท", self.toggle_action),
                          ("ล็อก / ปลดล็อกตำแหน่ง", self.toggle_local)):
            ttk.Button(btns2, text=text, command=cmd).pack(side="left", padx=2)

        self.start_btn = ttk.Button(root, text="▶ เริ่มทำงาน (F8)", style="Big.TButton", command=self.toggle)
        self.start_btn.pack(fill="x", **pad)

        self.log = tk.Text(root, height=7, state="disabled", font=(FONT, 9), wrap="word")
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    # ---------- helpers ----------
    def write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n")
        self.log.delete("1.0", "end-300l")
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh_list(self):
        selected = self.selected_name()
        self.tree.delete(*self.tree.get_children())
        for t in self.store.items:
            self.tree.insert("", "end", iid=t.name, values=(
                t.name, "หยุดบอท" if t.action == "stop" else "กด", f"{t.threshold:.2f}",
                "✓" if t.local and t.box else "", "✓" if t.count_round else "",
                "✓" if t.enabled else "—"))
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)

    def selected_name(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def selected(self):
        name = self.selected_name()
        return self.store.get(name) if name else None

    def game_frame(self):
        hwnd = gamewin.find_game(self.settings["process"])
        if not hwnd or gamewin.is_minimized(hwnd):
            messagebox.showwarning("ไม่พบเกม", "เปิดเกมแบบหน้าต่าง (ไม่ย่อ) ก่อน")
            return None
        return gamewin.capture(hwnd)

    def apply_hide(self):
        hwnd = gamewin.find_game(self.settings["process"])
        if not hwnd:
            return
        try:
            if self.hide.get():
                gamewin.hide_window(hwnd)
            else:
                gamewin.show_window(hwnd)
        except Exception as exc:
            self.hide.set(False)
            if getattr(exc, "winerror", None) == 5:
                messagebox.showwarning("ต้องใช้สิทธิ์ Admin", "กด “เปิดใหม่แบบ Admin” ก่อน")
            else:
                self.write_log(f"ซ่อน/แสดงเกมไม่ได้: {exc!r}")

    # ---------- template actions ----------
    def add_template(self):
        frame = self.game_frame()
        if frame is not None:
            CaptureDialog(self, frame)

    def move(self, delta):
        if self.selected_name():
            self.store.move(self.selected_name(), delta)
            self.refresh_list()

    def toggle_enabled(self):
        tpl = self.selected()
        if tpl:
            tpl.enabled = not tpl.enabled
            self.store.save()
            self.refresh_list()

    def toggle_round(self):
        tpl = self.selected()
        if tpl:
            tpl.count_round = not tpl.count_round
            self.store.save()
            self.refresh_list()

    def toggle_action(self):
        tpl = self.selected()
        if tpl:
            tpl.action = "click" if tpl.action == "stop" else "stop"
            if tpl.action == "stop":
                tpl.count_round = False
            self.store.save()
            self.refresh_list()

    def toggle_local(self):
        tpl = self.selected()
        if not tpl:
            return
        if tpl.local:
            tpl.local = False
        else:
            if not tpl.box:   # captured before positions were recorded
                frame = self.game_frame()
                if frame is None:
                    return
                if not self.store.learn_position(tpl, frame):
                    messagebox.showinfo("ไม่เจอปุ่มบนจอ",
                                        f"เปิดหน้าเกมที่มีปุ่ม “{tpl.name}” ก่อน แล้วกดล็อกอีกครั้ง")
                    return
            tpl.local = True
        self.store.save()
        self.refresh_list()

    def edit_threshold(self):
        tpl = self.selected()
        if not tpl:
            return
        value = simpledialog.askfloat("ความแม่น", "0.50 – 0.99 (ยิ่งสูงยิ่งเข้มงวด)", parent=self.root,
                                      initialvalue=tpl.threshold, minvalue=0.5, maxvalue=0.99)
        if value:
            tpl.threshold = value
            self.store.save()
            self.refresh_list()

    def delete(self):
        name = self.selected_name()
        if name and messagebox.askyesno("ลบ", f"ลบปุ่ม “{name}”?"):
            self.store.remove(name)
            self.refresh_list()

    def test_match(self):
        frame = self.game_frame()
        if frame is None:
            return
        for tpl, score, (x, y) in self.store.scores(frame):
            mark = "เจอ" if score >= tpl.threshold else "ไม่เจอ"
            self.write_log(f"ทดสอบ {tpl.name}: {score:.2f} → {mark} ({x},{y})")

    # ---------- run control ----------
    def save_settings(self):
        try:
            rounds = max(1, int(self.max_rounds.get()))
        except (tk.TclError, ValueError):
            rounds = self.settings["max_rounds"]
        self.settings.update(max_rounds=rounds, unlimited=self.unlimited.get(),
                             click_mode=MODES[self.mode.get()])
        self.settings_path.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2),
                                      encoding="utf-8")

    def toggle(self):
        if self.bot and self.bot.is_alive():
            self.bot.stop()
            return
        if not any(t.enabled for t in self.store.items):
            messagebox.showinfo("ยังไม่มีปุ่ม", "กด “+ เพิ่มจากจอเกม” เพื่อสอนว่าต้องกดปุ่มไหนก่อน")
            return
        self.save_settings()
        self.round_label.configure(text="0")
        self.bot = Bot(self.store, self.settings, lambda kind, value: self.q.put((kind, value)),
                       self.base / "logs")
        self.bot.start()
        self.start_btn.configure(text="■ หยุด (F8)")
        self.write_log("เริ่มทำงาน")

    def poll(self):
        try:
            while True:
                kind, value = self.q.get_nowait()
                if kind == "hotkey":
                    self.toggle()
                elif kind == "status":
                    self.status.configure(text=value)
                elif kind == "log":
                    self.write_log(value)
                elif kind == "hidden":
                    self.hide.set(value)
                elif kind == "round":
                    total = "" if self.settings["unlimited"] else f" / {self.settings['max_rounds']}"
                    self.round_label.configure(text=f"{value}{total}")
                elif kind == "stopped":
                    self.status.configure(text=value)
                    self.write_log(value)
                    self.start_btn.configure(text="▶ เริ่มทำงาน (F8)")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def watch_game(self):
        hwnd = gamewin.find_game(self.settings["process"])
        if hwnd:
            w, h = gamewin.client_size(hwnd)
            note = " (ซ่อนอยู่)" if gamewin.is_hidden(hwnd) else ""
            self.game_label.configure(text=f"● พบเกม {w}×{h}{note}", foreground="#1a8f4c")
            # Game left invisible by a run that did not exit cleanly: bring it back.
            if not self.hide.get() and gamewin.is_ghost(hwnd):
                try:
                    gamewin.show_window(hwnd)
                except Exception:
                    pass
        else:
            self.game_label.configure(text="○ ไม่พบเกม", foreground="#b3261e")
        self.root.after(1500, self.watch_game)

    def relaunch_admin(self):
        args = " ".join(f'"{a}"' for a in sys.argv)
        if ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, str(self.base), 1) > 32:
            self.on_close()

    def on_close(self):
        if self.bot:
            self.bot.stop()
        self.save_settings()
        hwnd = gamewin.find_game(self.settings["process"])
        if hwnd:
            try:
                gamewin.show_window(hwnd)   # never leave the game stranded off-screen
            except Exception:
                pass
        gamewin.stop_capture()
        self.root.destroy()


def main(base_dir):
    gamewin.set_dpi_aware()
    root = tk.Tk()
    root.minsize(460, 640)
    App(root, base_dir)
    root.mainloop()
