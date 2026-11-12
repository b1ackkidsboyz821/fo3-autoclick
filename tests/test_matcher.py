import cv2
import numpy as np
import pytest

from fo3auto.matcher import TemplateStore


def make_frame(h=480, w=640, seed=1):
    rng = np.random.default_rng(seed)
    frame = rng.integers(0, 60, (h, w, 3), dtype=np.uint8)
    # a "button": bright box with a darker inner pattern
    cv2.rectangle(frame, (400, 300), (520, 340), (30, 200, 250), -1)
    cv2.putText(frame, "NEXT", (415, 330), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2)
    return frame


def test_finds_button_at_capture_position(tmp_path):
    frame = make_frame()
    store = TemplateStore(tmp_path)
    store.add("next", frame[300:341, 400:521].copy(), base_h=480)
    tpl, score, (x, y) = store.find_first(frame)
    assert tpl.name == "next"
    assert score > 0.99
    assert abs(x - 460) <= 1 and abs(y - 320) <= 1


def test_finds_button_in_resized_window(tmp_path):
    frame = make_frame()
    store = TemplateStore(tmp_path)
    store.add("next", frame[300:341, 400:521].copy(), base_h=480)
    big = cv2.resize(frame, (1280, 960), interpolation=cv2.INTER_LINEAR)
    tpl, score, (x, y) = store.find_first(big)
    assert score > 0.85
    assert abs(x - 920) <= 4 and abs(y - 640) <= 4


def test_no_match_when_button_absent(tmp_path):
    frame = make_frame()
    store = TemplateStore(tmp_path)
    store.add("next", frame[300:341, 400:521].copy(), base_h=480)
    empty = np.random.default_rng(7).integers(0, 60, (480, 640, 3), dtype=np.uint8)
    assert store.find_first(empty) is None


def test_priority_order_and_disabled(tmp_path):
    frame = make_frame()
    cv2.rectangle(frame, (50, 50), (150, 90), (200, 60, 60), -1)
    cv2.putText(frame, "OK", (70, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    store = TemplateStore(tmp_path)
    store.add("next", frame[300:341, 400:521].copy(), base_h=480)
    store.add("ok", frame[50:91, 50:151].copy(), base_h=480)
    assert store.find_first(frame)[0].name == "next"
    store.move("ok", -1)
    assert store.find_first(frame)[0].name == "ok"
    store.get("ok").enabled = False
    assert store.find_first(frame)[0].name == "next"


def test_persists_and_reloads_thai_names(tmp_path):
    frame = make_frame()
    store = TemplateStore(tmp_path)
    store.add("ต่อไป", frame[300:341, 400:521].copy(), base_h=480, count_round=True)
    again = TemplateStore(tmp_path)
    assert [t.name for t in again.items] == ["ต่อไป"]
    assert again.items[0].count_round is True
    assert again.find_first(frame)[0].name == "ต่อไป"


def test_duplicate_name_rejected(tmp_path):
    frame = make_frame()
    store = TemplateStore(tmp_path)
    store.add("next", frame[300:341, 400:521].copy(), base_h=480)
    with pytest.raises(ValueError):
        store.add("next", frame[300:341, 400:521].copy(), base_h=480)


def two_button_frame():
    """Two identical NEXT buttons at different places."""
    frame = make_frame()
    frame[100:141, 80:201] = frame[300:341, 400:521]
    return frame


def test_position_lock_separates_identical_buttons(tmp_path):
    frame = two_button_frame()
    store = TemplateStore(tmp_path)
    crop = frame[300:341, 400:521].copy()
    store.add("right", crop, base_h=480, box=[400, 300, 521, 341], base_w=640, local=True)
    store.add("left", crop, base_h=480, box=[80, 100, 201, 141], base_w=640, local=True)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    assert store.match(store.get("right"), gray)[1] == (460, 320)
    assert store.match(store.get("left"), gray)[1] == (140, 120)
    # only the left button on screen: "right" must not claim it
    only_left = make_frame()
    only_left[100:141, 80:201] = only_left[300:341, 400:521]
    only_left[290:350, 390:530] = 20
    assert store.find_first(only_left)[0].name == "left"


def test_position_lock_follows_window_resize(tmp_path):
    frame = two_button_frame()
    store = TemplateStore(tmp_path)
    store.add("left", frame[100:141, 80:201].copy(), base_h=480,
              box=[80, 100, 201, 141], base_w=640, local=True)
    big = cv2.resize(frame, (1280, 960), interpolation=cv2.INTER_LINEAR)
    tpl, score, (x, y) = store.find_first(big)
    assert score > 0.85
    assert abs(x - 280) <= 4 and abs(y - 240) <= 4


def test_position_lock_ignored_on_other_aspect_ratio(tmp_path):
    frame = make_frame()
    store = TemplateStore(tmp_path)
    store.add("next", frame[300:341, 400:521].copy(), base_h=480,
              box=[400, 300, 521, 341], base_w=640, local=True)
    wide = np.random.default_rng(3).integers(0, 60, (480, 860, 3), dtype=np.uint8)
    wide[300:341, 700:821] = frame[300:341, 400:521]   # button moved far right
    tpl, score, (x, y) = store.find_first(wide)
    assert (x, y) == (760, 320)


def test_stop_template_wins_over_click(tmp_path):
    frame = two_button_frame()
    store = TemplateStore(tmp_path)
    crop = frame[300:341, 400:521].copy()
    store.add("click me", crop, base_h=480)
    store.add("guard", crop, base_h=480, action="stop")
    assert store.find_first(frame)[0].name == "guard"
    store.get("guard").enabled = False
    assert store.find_first(frame)[0].name == "click me"


def test_learn_position_for_old_template(tmp_path):
    frame = make_frame()
    store = TemplateStore(tmp_path)
    store.add("next", frame[300:341, 400:521].copy(), base_h=480)
    tpl = store.get("next")
    assert tpl.box is None
    assert store.learn_position(tpl, frame)
    assert tpl.box == [400, 300, 521, 341] and tpl.base_w == 640
    empty = np.random.default_rng(7).integers(0, 60, (480, 640, 3), dtype=np.uint8)
    assert not store.learn_position(store.get("next"), empty)


def test_old_index_without_new_fields_loads(tmp_path):
    import json
    frame = make_frame()
    store = TemplateStore(tmp_path)
    store.add("next", frame[300:341, 400:521].copy(), base_h=480)
    old = [{k: v for k, v in row.items() if k not in ("action", "box", "base_w", "local")}
           for row in json.loads(store.index.read_text(encoding="utf-8"))]
    store.index.write_text(json.dumps(old), encoding="utf-8")
    again = TemplateStore(tmp_path)
    assert again.items[0].action == "click" and again.items[0].local is False
    assert again.find_first(frame)[0].name == "next"


def test_flat_template_does_not_crash(tmp_path):
    frame = make_frame()
    store = TemplateStore(tmp_path)
    store.add("flat", np.full((20, 20, 3), 128, np.uint8), base_h=480)
    score, _ = store.match(store.get("flat"), cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    assert np.isfinite(score)
