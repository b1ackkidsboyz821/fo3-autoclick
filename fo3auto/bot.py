"""The click loop: capture -> match -> click -> repeat."""
import threading
import time
from pathlib import Path

from . import gamewin
from .matcher import imwrite

# After this many clicks on the same button without the screen changing,
# suggest (in the log) that the game may be ignoring background clicks.
HINT_AFTER = 15


class Bot(threading.Thread):
    def __init__(self, store, settings, emit, log_dir):
        super().__init__(daemon=True)
        self.store = store
        self.settings = dict(settings)
        self.emit = emit              # emit(kind, value) -> GUI queue
        self.log_dir = Path(log_dir)
        self.stop_event = threading.Event()
        self.rounds = 0

    def stop(self):
        self.stop_event.set()

    def _wait(self, seconds):
        return self.stop_event.wait(seconds)

    def run(self):
        reason = "หยุดแล้ว"
        try:
            reason = self._loop() or reason
        except Exception as exc:  # report instead of dying silently
            if getattr(exc, "winerror", None) == 5:
                reason = "เกมรันแบบ Admin — กด “เปิดใหม่แบบ Admin” ก่อน"
            else:
                reason = f"ผิดพลาด: {exc!r}"
        self.emit("stopped", reason)

    def _loop(self):
        s = self.settings
        mode = s.get("click_mode", "background")
        max_rounds = 0 if s.get("unlimited") else int(s.get("max_rounds", 0))
        last_progress = time.time()
        last_name, repeats = None, 0

        while not self.stop_event.is_set():
            hwnd = gamewin.find_game(s["process"])
            if not hwnd:
                self.emit("status", "ไม่พบเกม — รอ")
                self._wait(2)
                continue
            if gamewin.is_minimized(hwnd):
                # A minimized game stops drawing. Make it invisible instead:
                # still out of the user's way, but it keeps running.
                self.emit("log", "เกมถูกย่อ — เปลี่ยนเป็นซ่อนแบบโปร่งใสเพื่อให้ทำงานต่อได้")
                gamewin.hide_window(hwnd)
                self.emit("hidden", True)
                self._wait(1)
                continue

            frame = gamewin.capture(hwnd)
            hit = self.store.find_first(frame) if frame is not None else None

            # Progress = a different button got clicked. Waiting on an empty
            # screen or hammering one button forever both count as stuck.
            stuck = float(s.get("stuck_seconds", 300))
            if stuck and time.time() - last_progress > stuck:
                path = self._save_stuck(frame)
                return f"ค้างเกิน {int(stuck)} วินาที — บันทึกภาพที่ {path}"

            if hit is None:
                last_name, repeats = None, 0
                self.emit("status", "รอปุ่ม…")
                self._wait(float(s.get("interval", 0.6)))
                continue

            tpl, score, (x, y) = hit
            if tpl.action == "stop":
                path = self._save_shot(frame, "stop")
                return f"เจอ “{tpl.name}” ({score:.2f}) — หยุดตามที่ตั้งไว้ ภาพ: {path}"
            repeats = repeats + 1 if tpl.name == last_name else 1
            # Never take over the mouse on our own: the user picked the mode.
            # A button that stays up (slow screen change) is normal, so only hint.
            if mode == "background" and repeats == HINT_AFTER:
                self.emit("log", f"กด “{tpl.name}” {HINT_AFTER} ครั้งแล้วจอไม่เปลี่ยน — "
                                 "ถ้าเกมไม่รับคลิก ลองเปลี่ยนวิธีคลิกเป็น “คลิกจริง”")

            if mode == "background":
                gamewin.click_background(hwnd, x, y)
            elif gamewin.is_hidden(hwnd):
                # a real click would pass through the hidden window
                return "โหมดคลิกจริงใช้กับเกมที่ซ่อนอยู่ไม่ได้ — เอาติ๊ก “ซ่อนหน้าต่างเกม” ออก"
            else:
                gamewin.click_foreground(hwnd, x, y)
            self.emit("status", f"กด: {tpl.name}")
            self.emit("log", f"กด {tpl.name} ({score:.2f}) ที่ {x},{y}")

            # Count a round once per appearance of the button, not per click.
            if tpl.count_round and tpl.name != last_name:
                self.rounds += 1
                self.emit("round", self.rounds)
                if max_rounds and self.rounds >= max_rounds:
                    return f"ครบ {max_rounds} รอบ"
            if tpl.name != last_name:
                last_progress = time.time()
            last_name = tpl.name
            self._wait(float(s.get("after_click", 1.0)))
        return None

    def _save_stuck(self, frame):
        return self._save_shot(frame, "stuck")

    def _save_shot(self, frame, kind):
        if frame is None:
            return "-"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / time.strftime(f"{kind}_%Y%m%d_%H%M%S.png")
        imwrite(path, frame)
        return path
