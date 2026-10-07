"""Work Dictate - offline push-to-dictate for Windows.

Press the hotkey (default Ctrl+Alt+Space) to start listening, press it again
to stop. Each phrase is transcribed locally with Whisper as soon as you pause
and typed at the cursor of whichever window has focus. Esc also stops.
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
import queue

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
    "pause_seconds": 0.6,
    "max_phrase_seconds": 15,
    "type_method": "keys",
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
        self.audio_q = queue.Queue()
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
        self.audio_q.put(indata[:, 0].copy())

    def toggle(self):
        with self.lock:
            if self.model is None:
                return
            if not self.recording:
                self.audio_q = queue.Queue()
                try:
                    self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32",
                                                 blocksize=RATE // 10, callback=self.on_audio)
                    self.stream.start()
                except Exception as e:
                    log.exception("mic open failed")
                    self.set_state("idle", f"mic error: {e}")
                    return
                self.recording = True
                self.beep(880)
                self.set_state("recording", "listening... (hotkey or Esc to stop)")
                threading.Thread(target=self.listen, args=(self.audio_q,), daemon=True).start()
            else:
                self.stop()

    def cancel(self):
        with self.lock:
            if self.recording:
                self.stop()

    def stop(self):
        self.recording = False
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.audio_q.put(None)  # flush whatever is left
        self.beep(660)

    def listen(self, q):
        """Cut the mic stream into phrases at pauses and transcribe each one."""
        pause_blocks = int(self.cfg["pause_seconds"] * 10)
        max_blocks = int(self.cfg["max_phrase_seconds"] * 10)
        noise = 0.003
        phrase, speaking, quiet = [], False, 0
        while True:
            block = q.get()
            if block is None:
                if speaking:
                    self.transcribe(np.concatenate(phrase))
                break
            rms = float(np.sqrt(np.mean(block ** 2)))
            loud = rms > max(0.006, noise * 3)
            if not loud:
                noise = 0.95 * noise + 0.05 * rms
            if not speaking:
                phrase = (phrase + [block])[-3:]  # keep 0.3s of lead-in
                if loud:
                    speaking, quiet = True, 0
                continue
            phrase.append(block)
            quiet = 0 if loud else quiet + 1
            if quiet >= pause_blocks or len(phrase) >= max_blocks:
                self.transcribe(np.concatenate(phrase))
                phrase, speaking, quiet = [], False, 0
        self.set_state("idle", f"ready ({self.cfg['hotkey']})")

    def transcribe(self, audio):
        if len(audio) < RATE * 0.4:
            return
        if self.recording:
            self.set_state("busy", "listening... (writing)")
        try:
            t = time.time()
            segments, _ = self.model.transcribe(
                audio, language=self.cfg["language"], beam_size=self.cfg["beam_size"],
                vad_filter=True, condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segments).strip()
            log.info("%.1fs audio -> %.1fs: %r", len(audio) / RATE, time.time() - t, text)
            if text:
                self.write(text + (" " if self.cfg["trailing_space"] else ""))
        except Exception:
            log.exception("transcribe failed")
        if self.recording:
            self.set_state("recording", "listening... (hotkey or Esc to stop)")

    def write(self, text):
        # wait for any held modifiers so they don't combine with the typed keys
        for _ in range(40):
            if not any(keyboard.is_pressed(k) for k in ("ctrl", "alt", "shift", "windows")):
                break
            time.sleep(0.05)
        if self.cfg["type_method"] == "paste":
            self.paste(text)
        else:
            keyboard.write(text)

    def paste(self, text):
        try:
            old = pyperclip.paste()
        except Exception:
            old = None
        pyperclip.copy(text)
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
