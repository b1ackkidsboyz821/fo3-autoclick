"""Button templates and template matching."""
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np


def imread(path):
    """cv2.imread that also works with non-ASCII Windows paths."""
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def imwrite(path, img):
    ok, buf = cv2.imencode(Path(path).suffix or ".png", img)
    if ok:
        buf.tofile(str(path))
    return ok


@dataclass
class Template:
    name: str
    file: str
    base_h: int              # game client height when the template was captured
    threshold: float = 0.85
    count_round: bool = False
    enabled: bool = True
    action: str = "click"    # "click", or "stop" = halt the bot when seen
    box: list = None         # [x0, y0, x1, y1] where it was captured (base size)
    base_w: int = 0          # game client width when captured
    local: bool = False      # search only around `box` instead of the whole frame
    gray: np.ndarray = field(default=None, repr=False, compare=False)

    def to_json(self):
        return {"name": self.name, "file": self.file, "base_h": self.base_h,
                "threshold": self.threshold, "count_round": self.count_round,
                "enabled": self.enabled, "action": self.action, "box": self.box,
                "base_w": self.base_w, "local": self.local}


class TemplateStore:
    """Ordered list of templates; the order is the click priority."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.index = self.folder / "index.json"
        self.items = []
        self._scaled = {}
        self.load()

    def load(self):
        self.items = []
        if not self.index.exists():
            return
        for row in json.loads(self.index.read_text(encoding="utf-8")):
            img = imread(self.folder / row["file"])
            if img is None:
                continue
            tpl = Template(**row)
            tpl.gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            self.items.append(tpl)

    def save(self):
        rows = [t.to_json() for t in self.items]
        self.index.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, name):
        return next((t for t in self.items if t.name == name), None)

    def add(self, name, bgr, base_h, threshold=0.85, count_round=False,
            action="click", box=None, base_w=0, local=False):
        if self.get(name):
            raise ValueError(f"duplicate template name: {name}")
        file = f"{uuid.uuid4().hex[:10]}.png"
        imwrite(self.folder / file, bgr)
        tpl = Template(name, file, int(base_h), threshold, count_round, True,
                       action, box, int(base_w), local)
        tpl.gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        self.items.append(tpl)
        self.save()
        return tpl

    def remove(self, name):
        tpl = self.get(name)
        if tpl:
            self.items.remove(tpl)
            (self.folder / tpl.file).unlink(missing_ok=True)
            self._scaled.clear()
            self.save()

    def move(self, name, delta):
        tpl = self.get(name)
        if not tpl:
            return
        i = self.items.index(tpl)
        j = max(0, min(len(self.items) - 1, i + delta))
        self.items.insert(j, self.items.pop(i))
        self.save()

    def _for_height(self, tpl, frame_h):
        """Template resized for the current window height (the UI scales with it)."""
        key = (tpl.file, frame_h)
        if key not in self._scaled:
            scale = frame_h / tpl.base_h
            if abs(scale - 1) < 0.01:
                img = tpl.gray
            else:
                h, w = tpl.gray.shape
                size = (max(1, round(w * scale)), max(1, round(h * scale)))
                interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
                img = cv2.resize(tpl.gray, size, interpolation=interp)
            self._scaled[key] = img
        return self._scaled[key]

    def search_area(self, tpl, fw, fh):
        """(x0, y0, x1, y1) to search for a position-locked template, else None.

        Only used when the window has the aspect ratio the template was
        captured at; with another ratio the UI is laid out differently, so
        the whole frame is searched instead.
        """
        if not (tpl.local and tpl.box and tpl.base_w):
            return None
        if abs(fw / fh - tpl.base_w / tpl.base_h) > 0.02:
            return None
        s = fh / tpl.base_h
        x0, y0, x1, y1 = (v * s for v in tpl.box)
        mx = max(30 * s, (x1 - x0) * 0.5)
        my = max(30 * s, (y1 - y0) * 0.5)
        return (max(0, int(x0 - mx)), max(0, int(y0 - my)),
                min(fw, int(x1 + mx) + 1), min(fh, int(y1 + my) + 1))

    def match(self, tpl, frame_gray, whole_frame=False):
        """Return (score, (center_x, center_y)) of the best match."""
        img = self._for_height(tpl, frame_gray.shape[0])
        th, tw = img.shape
        fh, fw = frame_gray.shape
        area = None if whole_frame else self.search_area(tpl, fw, fh)
        ox, oy, search = 0, 0, frame_gray
        if area:
            ox, oy, ax1, ay1 = area
            search = frame_gray[oy:ay1, ox:ax1]
        sh, sw = search.shape
        if th > sh or tw > sw:
            return 0.0, (0, 0)
        res = cv2.matchTemplate(search, img, cv2.TM_CCOEFF_NORMED)
        res[~np.isfinite(res)] = 0  # flat templates give NaN/inf
        _, score, _, (x, y) = cv2.minMaxLoc(res)
        return float(score), (ox + x + tw // 2, oy + y + th // 2)

    def learn_position(self, tpl, frame_bgr):
        """Record where `tpl` currently is on screen. False if it is not there."""
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        score, (cx, cy) = self.match(tpl, gray, whole_frame=True)
        if score < tpl.threshold:
            return False
        fh, fw = gray.shape
        s = fh / tpl.base_h
        th, tw = tpl.gray.shape
        x0, y0 = int(cx / s - tw / 2 + 0.5), int(cy / s - th / 2 + 0.5)
        tpl.box = [x0, y0, x0 + tw, y0 + th]
        tpl.base_w = round(fw / s)
        return True

    def scores(self, frame_bgr):
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        return [(t, *self.match(t, gray)) for t in self.items]

    def find_first(self, frame_bgr):
        """First enabled template that is on screen.

        "stop" templates are checked before any click, whatever their place
        in the list, so a guard screen always wins over a button on it.
        """
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        active = [t for t in self.items if t.enabled]
        ordered = ([t for t in active if t.action == "stop"]
                   + [t for t in active if t.action != "stop"])
        for tpl in ordered:
            score, pos = self.match(tpl, gray)
            if score >= tpl.threshold:
                return tpl, score, pos
        return None
