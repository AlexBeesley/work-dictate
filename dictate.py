"""Work Dictate - offline push-to-dictate for Windows.

Press the hotkey (default Ctrl+Alt+Space) to start recording, press it again
to stop. The speech is transcribed locally with Whisper and pasted into
whichever window has focus. Esc while recording cancels.
"""
import ctypes
import json
import logging
import os
import sys
import threading
import time
import winsound

import keyboard
import numpy as np
import pyperclip
import pystray
import sounddevice as sd
from PIL import Image, ImageDraw

APP = "Work Dictate"
RATE = 16000

BASE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
DEFAULTS = {
    "hotkey": "ctrl+alt+space",
    "model": "models/small.en",
    "language": "en",
    "beam_size": 1,
    "beeps": True,
    "trailing_space": True,
}

logging.basicConfig(
    filename=os.path.join(BASE, "dictate.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(APP)


def load_config():
    path = os.path.join(BASE, "settings.json")
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            log.exception("bad settings.json, using defaults")
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS, f, indent=2)
    return cfg


def icon_image(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((22, 6, 42, 38), radius=10, fill=color)
    d.arc((14, 18, 50, 48), start=0, end=180, fill=color, width=4)
    d.line((32, 48, 32, 58), fill=color, width=4)
    d.line((22, 58, 42, 58), fill=color, width=4)
    return img


ICONS = {
    "loading": icon_image((130, 130, 130, 255)),
    "idle": icon_image((60, 160, 255, 255)),
    "recording": icon_image((235, 50, 50, 255)),
    "busy": icon_image((245, 170, 30, 255)),
}


class Dictate:
    def __init__(self, cfg):
        self.cfg = cfg
        self.model = None
        self.recording = False
        self.frames = []
        self.stream = None
        self.lock = threading.Lock()
        self.tray = pystray.Icon(APP, ICONS["loading"], f"{APP} - loading model...", menu=pystray.Menu(
            pystray.MenuItem(lambda _: f"Hotkey: {cfg['hotkey']}", None, enabled=False),
            pystray.MenuItem("Start / stop dictation", lambda: self.toggle(), default=True),
            pystray.MenuItem("Open settings.json", lambda: os.startfile(os.path.join(BASE, "settings.json"))),
            pystray.MenuItem("Quit", lambda: self.quit()),
        ))

    def set_state(self, state, tip):
        self.tray.icon = ICONS[state]
        self.tray.title = f"{APP} - {tip}"

    def beep(self, freq):
        if self.cfg["beeps"]:
            threading.Thread(target=winsound.Beep, args=(freq, 80), daemon=True).start()

    def load_model(self):
        try:
            from faster_whisper import WhisperModel
            path = self.cfg["model"]
            if not os.path.isabs(path):
                path = os.path.join(BASE, path)
            t = time.time()
            self.model = WhisperModel(path, device="cpu", compute_type="int8",
                                      cpu_threads=max(1, (os.cpu_count() or 4) - 1))
            log.info("model loaded from %s in %.1fs", path, time.time() - t)
            self.set_state("idle", f"ready ({self.cfg['hotkey']})")
        except Exception as e:
            log.exception("model load failed")
            self.set_state("loading", f"model failed: {e}")

    def on_audio(self, indata, frames, t, status):
        self.frames.append(indata[:, 0].copy())

    def toggle(self):
        with self.lock:
            if self.model is None:
                return
            if not self.recording:
                self.frames = []
                try:
                    self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32",
                                                 callback=self.on_audio)
                    self.stream.start()
                except Exception as e:
                    log.exception("mic open failed")
                    self.set_state("idle", f"mic error: {e}")
                    return
                self.recording = True
                self.beep(880)
                self.set_state("recording", "listening... (hotkey to stop, Esc to cancel)")
            else:
                self.stop_stream()
                self.beep(660)
                audio = np.concatenate(self.frames) if self.frames else np.zeros(0, np.float32)
                threading.Thread(target=self.transcribe, args=(audio,), daemon=True).start()

    def cancel(self):
        with self.lock:
            if self.recording:
                self.stop_stream()
                self.beep(440)
                self.set_state("idle", f"cancelled ({self.cfg['hotkey']})")

    def stop_stream(self):
        self.recording = False
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def transcribe(self, audio):
        if len(audio) < RATE * 0.3:
            self.set_state("idle", f"ready ({self.cfg['hotkey']})")
            return
        self.set_state("busy", "transcribing...")
        try:
            t = time.time()
            segments, _ = self.model.transcribe(
                audio, language=self.cfg["language"], beam_size=self.cfg["beam_size"],
                vad_filter=True, condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segments).strip()
            log.info("%.1fs audio -> %.1fs: %r", len(audio) / RATE, time.time() - t, text)
            if text:
                self.paste(text + (" " if self.cfg["trailing_space"] else ""))
        except Exception as e:
            log.exception("transcribe failed")
            self.set_state("idle", f"error: {e}")
            return
        self.set_state("idle", f"ready ({self.cfg['hotkey']})")

    def paste(self, text):
        try:
            old = pyperclip.paste()
        except Exception:
            old = None
        pyperclip.copy(text)
        # wait for the hotkey's modifiers to be released so they don't combine with Ctrl+V
        for _ in range(40):
            if not any(keyboard.is_pressed(k) for k in ("alt", "shift", "windows")):
                break
            time.sleep(0.05)
        keyboard.send("ctrl+v")
        time.sleep(0.4)
        if old is not None:
            try:
                pyperclip.copy(old)
            except Exception:
                pass

    def quit(self):
        self.cancel()
        keyboard.unhook_all()
        self.tray.stop()

    def run(self):
        keyboard.add_hotkey(self.cfg["hotkey"], self.toggle, suppress=True)
        keyboard.add_hotkey("esc", lambda: self.recording and self.cancel())
        threading.Thread(target=self.load_model, daemon=True).start()
        self.tray.run()


def single_instance():
    ctypes.windll.kernel32.CreateMutexW(None, False, "WorkDictateSingleInstance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    if not single_instance():
        ctypes.windll.user32.MessageBoxW(None, "Work Dictate is already running (see the tray).", APP, 0x40)
        sys.exit(0)
    try:
        Dictate(load_config()).run()
    except Exception:
        log.exception("fatal")
        raise
