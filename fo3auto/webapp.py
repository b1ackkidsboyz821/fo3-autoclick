"""HTML UI (pywebview, Edge WebView2). Same features as the Tk GUI in gui.py.

Python -> page: window.evaluate_js("fo3.event(kind, value)").
Page -> Python: window.pywebview.api.<method>(...), each returns plain JSON;
{"error": text} is shown to the user as a toast.
"""
import base64
import ctypes
import json
import queue
import re
import shutil
import sys
import threading
import time
from pathlib import Path

import cv2
import webview

from . import gamewin
from .bot import Bot
from .gui import DEFAULTS, FIRST_PROFILE, hotkey_loop
from .matcher import TemplateStore, imread

UI = Path(__file__).resolve().parent / "ui" / "index.html"
BAD_NAME = re.compile(r'[\\/:*?"<>|]')


def data_url(bgr, ext=".png", quality=None):
    params = [cv2.IMWRITE_JPEG_QUALITY, quality] if quality else []
    ok, buf = cv2.imencode(ext, bgr, params)
    kind = "jpeg" if ext == ".jpg" else "png"
    return f"data:image/{kind};base64," + base64.b64encode(buf.tobytes()).decode()


class Api:
    def __init__(self, base_dir):
        self._base = Path(base_dir)
        self._settings_path = self._base / "settings.json"
        self._settings = dict(DEFAULTS)
        if self._settings_path.exists():
            try:
                self._settings.update(json.loads(self._settings_path.read_text(encoding="utf-8")))
            except ValueError:
                pass
        self._profiles = self._base / "profiles"
        self._init_profiles()
        self._store = TemplateStore(self._profiles / self._settings["profile"])
        self._bot = None
        self._frame = None          # screenshot shown in the capture dialog
        self._hidden = False
        self._q = queue.Queue()
        self._lock = threading.Lock()
        self._window = None

    # ---------- plumbing ----------
    def _attach(self, window):
        self._window = window
        threading.Thread(target=hotkey_loop, args=(lambda: self._q.put(("hotkey", None)),),
                         daemon=True).start()
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._watch_game, daemon=True).start()

    def _emit(self, kind, value):
        if self._window:
            self._window.evaluate_js(f"window.fo3 && fo3.event({json.dumps(kind)}, "
                                     f"{json.dumps(value, ensure_ascii=False)})")

    def _log(self, text):
        self._emit("log", f"{time.strftime('%H:%M:%S')}  {text}")

    def _pump(self):
        """Bot thread and hotkey -> page."""
        while True:
            kind, value = self._q.get()
            try:
                if kind == "hotkey":
                    r = self.toggle_run()
                    if r.get("error"):
                        self._emit("toast", r["error"])
                elif kind == "log":
                    self._log(value)
                elif kind == "hidden":
                    self._hidden = bool(value)
                    self._emit("hidden", self._hidden)
                elif kind == "round":
                    self._emit("round", value)
                elif kind == "stopped":
                    self._log(value)
                    self._emit("status", value)
                    self._emit("running", False)
                else:
                    self._emit(kind, value)
            except Exception:
                pass

    def _watch_game(self):
        last = None
        while True:
            hwnd = gamewin.find_game(self._settings["process"])
            if hwnd:
                w, h = gamewin.client_size(hwnd)
                game = {"found": True, "w": w, "h": h, "hidden": gamewin.is_hidden(hwnd)}
                # game left invisible by a run that did not exit cleanly: bring it back
                if not self._hidden and gamewin.is_ghost(hwnd):
                    try:
                        gamewin.show_window(hwnd)
                    except Exception:
                        pass
            else:
                game = {"found": False}
            if game != last:
                self._emit("game", game)
                last = game
            time.sleep(1.5)

    def _running(self):
        return bool(self._bot and self._bot.is_alive())

    def _save_settings(self):
        self._settings_path.write_text(json.dumps(self._settings, ensure_ascii=False, indent=2),
                                       encoding="utf-8")

    # ---------- profiles ----------
    def _profile_names(self):
        return sorted(p.name for p in self._profiles.iterdir() if p.is_dir())

    def _init_profiles(self):
        self._profiles.mkdir(exist_ok=True)
        old = self._base / "templates"
        if (old / "index.json").exists() and not self._profile_names():
            old.rename(self._profiles / FIRST_PROFILE)
        if not self._profile_names():
            (self._profiles / FIRST_PROFILE).mkdir()
        if self._settings.get("profile") not in self._profile_names():
            self._settings["profile"] = self._profile_names()[0]

    def _load_profile(self, name):
        self._settings["profile"] = name
        self._store = TemplateStore(self._profiles / name)
        self._save_settings()

    def _check_name(self, name):
        name = (name or "").strip().rstrip(".")
        if not name:
            return None, "ใส่ชื่อก่อน"
        if BAD_NAME.search(name):
            return None, 'ห้ามมีอักขระ \\ / : * ? " < > |'
        if name.lower() in (n.lower() for n in self._profile_names()):
            return None, "มีโหมดชื่อนี้แล้ว"
        return name, None

    def _busy(self):
        return {"error": "หยุดบอทก่อน"} if self._running() else None

    # ---------- page API ----------
    def state(self):
        hwnd = gamewin.find_game(self._settings["process"])
        return {
            "admin": gamewin.is_admin(),
            "profiles": self._profile_names(),
            "profile": self._settings["profile"],
            "settings": {k: self._settings[k] for k in ("max_rounds", "unlimited", "click_mode")},
            "items": self.items(),
            "running": self._running(),
            "hidden": self._hidden,
            "game": ({"found": True, "w": gamewin.client_size(hwnd)[0], "h": gamewin.client_size(hwnd)[1],
                      "hidden": gamewin.is_hidden(hwnd)} if hwnd else {"found": False}),
        }

    def items(self):
        out = []
        for t in self._store.items:
            thumb = None
            if t.gray is not None:
                try:
                    img = imread(self._store.folder / t.file)
                    if img is not None:
                        thumb = data_url(img)
                except Exception:
                    pass
            out.append({"name": t.name, "action": t.action, "threshold": t.threshold,
                        "local": bool(t.local and t.box), "once": t.once,
                        "count_round": t.count_round, "enabled": t.enabled, "thumb": thumb})
        return out

    def set_option(self, key, value):
        if key == "max_rounds":
            try:
                self._settings["max_rounds"] = max(1, min(9999, int(value)))
            except (TypeError, ValueError):
                return {"error": "จำนวนรอบต้องเป็นตัวเลข"}
        elif key == "unlimited":
            self._settings["unlimited"] = bool(value)
        elif key == "click_mode" and value in ("background", "foreground"):
            self._settings["click_mode"] = value
        else:
            return {"error": f"unknown option {key}"}
        self._save_settings()
        return {"ok": True}

    def select_profile(self, name):
        if self._busy():
            return self._busy()
        if name not in self._profile_names():
            return {"error": "ไม่พบโหมดนี้"}
        self._load_profile(name)
        return self.state()

    def new_profile(self, name):
        if self._busy():
            return self._busy()
        name, err = self._check_name(name)
        if err:
            return {"error": err}
        (self._profiles / name).mkdir()
        self._load_profile(name)
        return self.state()

    def rename_profile(self, name):
        if self._busy():
            return self._busy()
        name, err = self._check_name(name)
        if err:
            return {"error": err}
        (self._profiles / self._settings["profile"]).rename(self._profiles / name)
        self._load_profile(name)
        return self.state()

    def delete_profile(self):
        if self._busy():
            return self._busy()
        shutil.rmtree(self._profiles / self._settings["profile"])
        if not self._profile_names():
            (self._profiles / FIRST_PROFILE).mkdir()
        self._load_profile(self._profile_names()[0])
        return self.state()

    # ---------- buttons ----------
    def _tpl(self, name):
        tpl = self._store.get(name)
        if not tpl:
            raise KeyError(name)
        return tpl

    def move(self, name, delta):
        self._store.move(name, int(delta))
        return self.items()

    def toggle(self, name, field):
        tpl = self._tpl(name)
        if field == "enabled":
            tpl.enabled = not tpl.enabled
        elif field == "count_round":
            tpl.count_round = not tpl.count_round
        elif field == "once":
            tpl.once = not tpl.once
        elif field == "action":
            # click -> Space key -> stop the bot -> click
            tpl.action = {"click": "space", "space": "stop"}.get(tpl.action, "click")
            if tpl.action == "stop":
                tpl.count_round = False
        elif field == "local":
            if tpl.local:
                tpl.local = False
            else:
                if not tpl.box:   # captured before positions were recorded
                    frame = self._game_frame()
                    if isinstance(frame, dict):
                        return frame
                    if not self._store.learn_position(tpl, frame):
                        return {"error": f"เปิดหน้าเกมที่มีปุ่ม “{tpl.name}” ก่อน แล้วกดล็อกอีกครั้ง"}
                tpl.local = True
        else:
            return {"error": f"unknown field {field}"}
        self._store.save()
        return self.items()

    def set_threshold(self, name, value):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return {"error": "ใส่ตัวเลข 0.50 – 0.99"}
        if not 0.5 <= v <= 0.99:
            return {"error": "ใส่ตัวเลข 0.50 – 0.99"}
        self._tpl(name).threshold = round(v, 2)
        self._store.save()
        return self.items()

    def delete(self, name):
        self._store.remove(name)
        return self.items()

    def _game_frame(self):
        hwnd = gamewin.find_game(self._settings["process"])
        if not hwnd or gamewin.is_minimized(hwnd):
            return {"error": "เปิดเกมแบบหน้าต่าง (ไม่ย่อ) ก่อน"}
        frame = gamewin.capture(hwnd)
        if frame is None:
            return {"error": "จับภาพเกมไม่ได้ ลองใหม่"}
        return frame

    def capture(self):
        frame = self._game_frame()
        if isinstance(frame, dict):
            return frame
        self._frame = frame
        h, w = frame.shape[:2]
        return {"img": data_url(frame, ".jpg", 90), "w": w, "h": h}

    def save_capture(self, name, box, count_round, stop, local):
        frame = self._frame
        if frame is None:
            return {"error": "จับภาพใหม่อีกครั้ง"}
        name = (name or "").strip()
        if not name:
            return {"error": "ตั้งชื่อปุ่มก่อน"}
        fh, fw = frame.shape[:2]
        x0, x1 = sorted(int(v) for v in (box[0], box[2]))
        y0, y1 = sorted(int(v) for v in (box[1], box[3]))
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(fw, x1), min(fh, y1)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return {"error": "ลากกรอบให้ใหญ่กว่านี้"}
        try:
            self._store.add(name, frame[y0:y1, x0:x1].copy(), fh,
                            count_round=bool(count_round) and not stop,
                            action="stop" if stop else "click",
                            box=[x0, y0, x1, y1], base_w=fw, local=bool(local))
        except ValueError:
            return {"error": "มีปุ่มชื่อนี้แล้ว"}
        self._frame = None
        return self.items()

    def test(self):
        frame = self._game_frame()
        if isinstance(frame, dict):
            return frame
        for tpl, score, (x, y) in self._store.scores(frame):
            mark = "เจอ" if score >= tpl.threshold else "ไม่เจอ"
            self._log(f"ทดสอบ {tpl.name}: {score:.2f} → {mark} ({x},{y})")
        return {"ok": True}

    # ---------- run ----------
    def toggle_run(self):
        with self._lock:
            if self._running():
                self._bot.stop()
                return {"running": False}
            if not any(t.enabled for t in self._store.items):
                return {"error": "กด “เพิ่มจากจอเกม” เพื่อสอนว่าต้องกดปุ่มไหนก่อน"}
            self._save_settings()
            self._emit("round", 0)
            self._bot = Bot(self._store, self._settings, lambda k, v: self._q.put((k, v)),
                            self._base / "logs")
            self._bot.start()
            self._log("เริ่มทำงาน")
            self._emit("running", True)
            return {"running": True}

    def set_hide(self, hide):
        hwnd = gamewin.find_game(self._settings["process"])
        if not hwnd:
            return {"error": "ไม่พบเกม"}
        try:
            if hide:
                gamewin.hide_window(hwnd)
            else:
                gamewin.show_window(hwnd)
            self._hidden = bool(hide)
            return {"hidden": self._hidden}
        except Exception as exc:
            if getattr(exc, "winerror", None) == 5:
                return {"error": "ต้องใช้สิทธิ์ Admin: กด “เปิดใหม่แบบ Admin” ก่อน"}
            return {"error": f"ซ่อน/แสดงเกมไม่ได้: {exc!r}"}

    def relaunch_admin(self):
        args = " ".join(f'"{a}"' for a in sys.argv)
        if ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, str(self._base), 1) > 32:
            self._window.destroy()
        return {"ok": True}

    def _on_closing(self):
        if self._bot:
            self._bot.stop()
        try:
            self._save_settings()
        except OSError:
            pass
        hwnd = gamewin.find_game(self._settings["process"])
        if hwnd:
            try:
                gamewin.show_window(hwnd)   # never leave the game invisible
            except Exception:
                pass
        gamewin.stop_capture()


def main(base_dir):
    gamewin.set_dpi_aware()
    api = Api(base_dir)
    window = webview.create_window("FO3 Auto Clicker", url=str(UI), js_api=api,
                                   width=1080, height=780, min_size=(760, 600),
                                   background_color="#0b1410")
    window.events.closing += api._on_closing
    window.events.loaded += lambda: api._attach(window) if api._window is None else None
    webview.start(gui="edgechromium", debug="--debug" in sys.argv)
