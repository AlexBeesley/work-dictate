# Work Dictate

Offline AI dictation for Windows, like Win+H but using Whisper (small.en) running locally on the CPU. It needs no internet, no admin rights, and no install.

## Use it

1. Download **WorkDictate.zip** from the [latest release](../../releases/latest) and extract it anywhere (e.g. `Documents\WorkDictate`).
2. Run `WorkDictate.exe`. A mic icon appears in the system tray: grey while loading, then blue when ready.
3. Click into any text box and press **Ctrl+Alt+Space**. You'll hear a beep and the icon turns red. Start talking.
4. Each phrase is typed at your cursor as soon as you pause, so text appears as you talk. The icon flashes amber while it writes.
5. Press **Ctrl+Alt+Space** again (or **Esc**) to stop. To quit, right-click the tray icon and choose Quit.

## Settings

`settings.json` is created next to the exe on first run:

| key | default | notes |
|---|---|---|
| `hotkey` | `ctrl+alt+space` | any `keyboard`-library combo, e.g. `ctrl+shift+d`, `f9` |
| `model` | `models/small.en` | any faster-whisper / CTranslate2 model folder |
| `beam_size` | `1` | 5 is a bit more accurate, and slower |
| `beeps` | `true` | start/stop sounds |
| `trailing_space` | `true` | adds a space after each phrase |
| `pause_seconds` | `0.6` | how long a pause ends a phrase; lower means text appears sooner |
| `max_phrase_seconds` | `15` | forces a phrase out if you talk without pausing |
| `type_method` | `keys` | `keys` types the text in; `paste` uses the clipboard (for apps that drop typed keys) |

Restart the app after editing. Errors are written to `dictate.log`.

## Notes

- By default text is typed in as keystrokes, so your clipboard isn't touched. With `paste`, your previous clipboard contents are restored afterwards.
- To autostart it, put a shortcut to the exe in `shell:startup`.
- Typing into windows running as admin won't work unless the app also runs as admin.

## Build

```
py -3.13 -m venv .venv
.venv\Scripts\pip install faster-whisper sounddevice numpy keyboard pystray pillow pyperclip pyinstaller
.venv\Scripts\python -c "from huggingface_hub import snapshot_download as d; d('Systran/faster-whisper-small.en', local_dir='models/small.en')"
.venv\Scripts\pyinstaller --noconfirm --windowed --name WorkDictate --icon icon.ico --collect-data faster_whisper --collect-binaries ctranslate2 --collect-binaries onnxruntime dictate.py
xcopy /e /i models dist\WorkDictate\models
```
