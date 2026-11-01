"""Push-to-talk questions: hold a key (or a wheel button), ask, let go.

The audio never leaves the PC. :class:`MicRecorder` captures the default microphone
with Qt Multimedia while the key is held, :class:`WhisperRecogniser` turns it into
text with faster-whisper on the CPU (the optional ``[voice]`` extra, imported
lazily; the model is downloaded on first use to ``<data dir>/models/whisper``), and
:class:`PushToTalk` hands the transcript to ``Hub.ask(text, "voice")``: the same
route as the typed question box, journalled with ``source: "voice"``.

The key is watched system-wide on Windows (``GetAsyncKeyState`` and, for a wheel
or button-box button, ``joyGetPosEx``), because iRacing has the focus while the
driver drives. Elsewhere only a key pressed in an RTV window counts.

While the radio speaks the microphone is muted (its samples are dropped), so the
pitwall's own voice through the speakers is never transcribed as the driver's.

``python -m rtv.desktop.voice --find-button`` prints which controller button is
pressed, for ``RTV_VOICE_KEY=joy<device>:<button>``.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtCore import QEvent, QObject, QTimer, Signal

SAMPLE_RATE = 16_000
MIN_SECONDS = 0.35  # shorter than this is a key tap, not a question
POLL_MS = 20
#: Words the driver is likely to say, as a hint to the recogniser.
PROMPT = (
    "Pitwall radio. How many laps of fuel? What's the gap ahead? Gap behind? "
    "Last lap time, best lap, tyre temps, laps to go, position. Should I box this lap? "
    "Pit, tyres, stint, undercut."
)

UNAVAILABLE_EXTRA = 'Voice needs the [voice] extra: pip install -e ".[voice]" (faster-whisper).'

# ---- the key ------------------------------------------------------------

_NAMED: dict[str, tuple[int, str | None]] = {
    # name: (Windows virtual-key code, Qt key name for the in-window fallback)
    "space": (0x20, "Space"),
    "tab": (0x09, "Tab"),
    "pause": (0x13, "Pause"),
    "scrolllock": (0x91, "ScrollLock"),
    "capslock": (0x14, "CapsLock"),
    "insert": (0x2D, "Ins"),
    "delete": (0x2E, "Del"),
    "home": (0x24, "Home"),
    "end": (0x23, "End"),
    "pageup": (0x21, "PgUp"),
    "pagedown": (0x22, "PgDown"),
    "backquote": (0xC0, "`"),
    "lctrl": (0xA2, None),
    "rctrl": (0xA3, None),
    "lshift": (0xA0, None),
    "rshift": (0xA1, None),
    "lalt": (0xA4, None),
    "ralt": (0xA5, None),
}


@dataclass(frozen=True)
class KeySpec:
    """A parsed ``RTV_VOICE_KEY``."""

    label: str
    vk: int | None = None  # Windows virtual-key code
    qt_name: str | None = None  # for the in-window fallback
    joystick: int | None = None  # winmm device id (0-based)
    button: int | None = None  # 1-based, as joy.cpl numbers them

    @property
    def is_joystick(self) -> bool:
        return self.joystick is not None


def parse_key(spec: str) -> KeySpec:
    """``"ScrollLock"``, ``"F13"``, ``"A"``, ``"Numpad0"``, ``"RCtrl"``, ``"joy1:5"``.

    Raises ``ValueError`` with the accepted forms for anything else.
    """
    raw = (spec or "").strip()
    name = raw.lower().replace(" ", "").replace("_", "")
    if name.startswith("joy"):
        rest = name[3:]
        device, sep, button = rest.partition(":")
        if not sep:
            device, button = "1", rest
        if device == "":
            device = "1"
        if device.isdigit() and button.isdigit() and 1 <= int(device) <= 16:
            if 1 <= int(button) <= 32:
                return KeySpec(
                    label=f"controller {device} button {button}",
                    joystick=int(device) - 1,
                    button=int(button),
                )
    elif name in _NAMED:
        vk, qt = _NAMED[name]
        return KeySpec(label=raw, vk=vk, qt_name=qt)
    elif len(name) == 1 and name.isalnum():
        return KeySpec(label=raw.upper(), vk=ord(name.upper()), qt_name=name.upper())
    elif name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
        n = int(name[1:])
        return KeySpec(label=f"F{n}", vk=0x6F + n, qt_name=f"F{n}")
    elif name.startswith("numpad") and name[6:].isdigit() and len(name) == 7:
        return KeySpec(label=raw, vk=0x60 + int(name[6:]))
    raise ValueError(
        f"RTV_VOICE_KEY={raw!r} is not a key RTV knows. Use a key name (ScrollLock, Pause, "
        "F1-F24, A-Z, 0-9, Numpad0-9, Space, RCtrl, RShift, RAlt...) or joy<device>:<button>, "
        "e.g. joy1:5."
    )


_JOYINFOEX_FIELDS = (
    "dwSize",
    "dwFlags",
    "dwXpos",
    "dwYpos",
    "dwZpos",
    "dwRpos",
    "dwUpos",
    "dwVpos",
    "dwButtons",
    "dwButtonNumber",
    "dwPOV",
    "dwReserved1",
    "dwReserved2",
)
_joyinfoex: Any = None


def _joy_info():
    """A JOYINFOEX asking for buttons (the struct is built on first use)."""
    global _joyinfoex
    import ctypes

    if _joyinfoex is None:

        class JOYINFOEX(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint32) for name in _JOYINFOEX_FIELDS]

        _joyinfoex = JOYINFOEX
    info = _joyinfoex()
    info.dwSize = ctypes.sizeof(info)
    info.dwFlags = 0x80  # JOY_RETURNBUTTONS
    return info


def joystick_buttons(device: int) -> int | None:
    """The pressed-button bitmask of winmm controller ``device``, or None if absent."""
    if sys.platform != "win32":
        return None
    import ctypes

    info = _joy_info()
    if ctypes.windll.winmm.joyGetPosEx(device, ctypes.byref(info)) != 0:
        return None
    return int(info.dwButtons)


def global_probe(key: KeySpec) -> Callable[[], bool] | None:
    """A function telling whether the key is held right now, system-wide (Windows)."""
    if sys.platform != "win32":
        return None
    import ctypes

    if key.is_joystick:
        bit = 1 << (key.button - 1)
        return lambda: bool((joystick_buttons(key.joystick) or 0) & bit)
    if key.vk is None:
        return None
    user32 = ctypes.windll.user32
    return lambda: bool(user32.GetAsyncKeyState(key.vk) & 0x8000)


class KeyWatcher(QObject):
    """Emits ``pressed`` / ``released`` on the push-to-talk key's edges.

    With a ``probe`` (Windows) it polls every 20 ms, so it works while another
    program has the focus. Without one it filters key events in RTV's own windows.
    """

    pressed = Signal()
    released = Signal()

    def __init__(
        self,
        key: KeySpec,
        probe: Callable[[], bool] | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.key = key
        self.probe = probe
        self.down = False
        self.global_ = probe is not None
        self._timer: QTimer | None = None
        self._qt_key: int | None = None

    def start(self) -> None:
        if self.probe is not None:
            self._timer = QTimer(self)
            self._timer.setInterval(POLL_MS)
            self._timer.timeout.connect(self.poll)
            self._timer.start()
            return
        if self.key.qt_name:
            from PySide6.QtGui import QKeySequence
            from PySide6.QtWidgets import QApplication

            seq = QKeySequence.fromString(self.key.qt_name)
            if seq.count():
                self._qt_key = seq[0].key()
                app = QApplication.instance()
                if app is not None:
                    app.installEventFilter(self)

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self._set(False)

    def poll(self) -> None:
        try:
            held = bool(self.probe()) if self.probe else False
        except Exception:  # noqa: BLE001 - a controller unplugged mid-race reads as released
            held = False
        self._set(held)

    def _set(self, held: bool) -> None:
        if held and not self.down:
            self.down = True
            self.pressed.emit()
        elif not held and self.down:
            self.down = False
            self.released.emit()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt API
        kind = event.type()
        if kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) and self._qt_key is not None:
            if event.key() == self._qt_key and not event.isAutoRepeat():
                self._set(kind == QEvent.Type.KeyPress)
                return True
        return False


# ---- audio --------------------------------------------------------------


def to_mono_16k(raw: bytes, rate: int, channels: int, sample_format: str):
    """PCM bytes from the microphone -> float32 mono at 16 kHz in [-1, 1]."""
    import numpy as np

    dtype = {"uint8": np.uint8, "int16": np.int16, "int32": np.int32, "float": np.float32}[
        sample_format
    ]
    usable = len(raw) - len(raw) % (np.dtype(dtype).itemsize * max(1, channels))
    data = np.frombuffer(raw[:usable], dtype=dtype)
    if sample_format == "uint8":
        samples = (data.astype(np.float32) - 128.0) / 128.0
    elif sample_format == "int16":
        samples = data.astype(np.float32) / 32768.0
    elif sample_format == "int32":
        samples = data.astype(np.float32) / 2147483648.0
    else:
        samples = data.astype(np.float32)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE and len(samples):
        n = int(round(len(samples) * SAMPLE_RATE / rate))
        x_old = np.arange(len(samples), dtype=np.float64) / rate
        x_new = np.arange(n, dtype=np.float64) / SAMPLE_RATE
        samples = np.interp(x_new, x_old, samples).astype(np.float32)
    return np.clip(samples, -1.0, 1.0).astype(np.float32)


class Recorder(Protocol):
    available: bool
    reason: str

    def start(self) -> None: ...
    def stop(self) -> Any: ...  # float32 mono 16 kHz samples
    def set_muted(self, muted: bool) -> None: ...


class MicRecorder(QObject):
    """The default microphone through Qt Multimedia, captured only while asked."""

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.available = False
        self.reason = ""
        self.muted = False
        self._chunks: list[bytes] = []
        self._source = None
        self._io = None
        self._format = None
        try:
            from PySide6.QtMultimedia import QMediaDevices
        except Exception as exc:  # noqa: BLE001 - Qt Multimedia missing or its libraries
            self.reason = f"No audio input on this system ({exc})."
            return
        self._devices = QMediaDevices
        if QMediaDevices.defaultAudioInput().isNull():
            self.reason = (
                "No microphone found. On Windows check Settings > Privacy & security >"
                " Microphone > Let desktop apps access your microphone."
            )
            return
        self.available = True

    def start(self) -> None:
        from PySide6.QtMultimedia import QAudioFormat, QAudioSource

        device = self._devices.defaultAudioInput()
        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not device.isFormatSupported(fmt):
            fmt = device.preferredFormat()
        self._format = fmt
        self._chunks = []
        self._source = QAudioSource(device, fmt, self)
        self._io = self._source.start()
        if self._io is not None:
            self._io.readyRead.connect(self._read)

    def _read(self) -> None:
        if self._io is None:
            return
        data = bytes(self._io.readAll().data())
        if data and not self.muted:
            self._chunks.append(data)

    def set_muted(self, muted: bool) -> None:
        if muted and not self.muted:
            self._read()  # keep what was said before the radio came on
        self.muted = muted

    def stop(self):
        if self._source is None:
            return None
        self._read()
        self._source.stop()
        fmt, self._source, self._io = self._format, None, None
        from PySide6.QtMultimedia import QAudioFormat

        names = {
            QAudioFormat.SampleFormat.UInt8: "uint8",
            QAudioFormat.SampleFormat.Int16: "int16",
            QAudioFormat.SampleFormat.Int32: "int32",
            QAudioFormat.SampleFormat.Float: "float",
        }
        raw, self._chunks = b"".join(self._chunks), []
        return to_mono_16k(
            raw, fmt.sampleRate(), fmt.channelCount(), names.get(fmt.sampleFormat(), "int16")
        )


# ---- speech to text -----------------------------------------------------


def whisper_installed() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


class Recogniser(Protocol):
    def ready(self) -> bool: ...
    def transcribe(self, audio: Any) -> str: ...


class WhisperRecogniser:
    """faster-whisper on the CPU (int8); loads, and if needed downloads, on first use."""

    def __init__(self, model: str, download_root: Path):
        self.model_name = model
        self.download_root = Path(download_root)
        self._model = None
        self._lock = threading.Lock()

    def ready(self) -> bool:
        return self._model is not None

    def downloaded(self) -> bool:
        root = self.download_root
        return root.is_dir() and any(root.glob(f"models--*faster-whisper-{self.model_name}"))

    def load(self) -> None:
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self.download_root.mkdir(parents=True, exist_ok=True)
                self._model = WhisperModel(
                    self.model_name,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(self.download_root),
                )

    def transcribe(self, audio) -> str:
        self.load()
        with self._lock:
            segments, _info = self._model.transcribe(
                audio,
                language="en",
                beam_size=5,
                vad_filter=True,
                condition_on_previous_text=False,
                initial_prompt=PROMPT,
            )
            words = [s.text.strip() for s in segments if getattr(s, "no_speech_prob", 0) < 0.6]
        return " ".join(w for w in words if w).strip()


# ---- the controller -----------------------------------------------------

Runner = Callable[[Callable[[], Any], Callable[[Any, BaseException | None], None]], None]


class _Bridge(QObject):
    done = Signal(object)

    def __init__(self):
        super().__init__()
        self.done.connect(self._deliver)

    def _deliver(self, packet) -> None:
        callback, result, error = packet
        callback(result, error)


def thread_runner() -> Runner:
    """Run ``work`` on a worker thread and ``done(result, error)`` back on the GUI thread."""
    bridge = _Bridge()

    def run(work, done):
        def target():
            try:
                packet = (done, work(), None)
            except BaseException as exc:  # noqa: BLE001 - reported to the UI, not raised
                packet = (done, None, exc)
            bridge.done.emit(packet)

        threading.Thread(target=target, name="rtv-voice", daemon=True).start()

    run.bridge = bridge  # keep it alive with the runner
    return run


def inline_runner(work, done) -> None:
    """For tests: run in place."""
    try:
        result = work()
    except BaseException as exc:  # noqa: BLE001
        done(None, exc)
        return
    done(result, None)


class PushToTalk(QObject):
    """Hold to record, release to transcribe, then emit the words.

    ``state_changed(state, detail)`` drives the talk button; ``state`` is one of
    ``off`` (unavailable; detail says why), ``idle``, ``listening``, ``muted``
    (listening, but the radio is on air), ``transcribing``, ``loading`` (first
    use: the model loads or downloads), ``heard`` (detail = the words, shown before
    the answer arrives), ``nothing`` (no words) and ``error``.
    ``transcribed(text)`` fires once per question, to ``Hub.ask(text, "voice")``.
    """

    state_changed = Signal(str, str)
    transcribed = Signal(str)

    def __init__(
        self,
        recorder: Recorder | None,
        recogniser: Recogniser | None,
        *,
        key_label: str = "",
        reason: str = "",
        runner: Runner | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.recorder = recorder
        self.recogniser = recogniser
        self.key_label = key_label
        self.runner = runner or thread_runner()
        self.radio_speaking = False
        self.recording = False
        self.busy = False
        if not reason and recogniser is None:
            reason = UNAVAILABLE_EXTRA
        if not reason and recorder is not None and not recorder.available:
            reason = recorder.reason
        if not reason and recorder is None:
            reason = "No audio input on this system."
        self.reason = reason
        self.available = not reason
        self.state = "idle" if self.available else "off"
        self.detail = "" if self.available else reason

    def _set(self, state: str, detail: str = "") -> None:
        self.state, self.detail = state, detail
        self.state_changed.emit(state, detail)

    def warm(self) -> None:
        """Load an already-downloaded model in the background, so the first answer is quick."""
        rec = self.recogniser
        if not self.available or rec is None or rec.ready():
            return
        if hasattr(rec, "downloaded") and not rec.downloaded():
            return  # downloading is for the first press, not for start-up
        if hasattr(rec, "load"):
            self.runner(rec.load, lambda _r, _e: None)

    def press(self) -> None:
        if not self.available or self.recording:
            return
        try:
            self.recorder.set_muted(self.radio_speaking)
            self.recorder.start()
        except Exception as exc:  # noqa: BLE001 - say it on the button, keep driving
            self._set("error", f"Microphone failed: {exc}")
            return
        self.recording = True
        self._set(*self._listening())

    def _listening(self) -> tuple[str, str]:
        if self.radio_speaking:
            return "muted", "Radio on air: mic muted until it ends."
        return "listening", "Listening… release to ask."

    def set_radio_speaking(self, speaking: bool) -> None:
        """The radio started or stopped speaking: mute the microphone meanwhile."""
        speaking = bool(speaking)
        if speaking == self.radio_speaking:
            return
        self.radio_speaking = speaking
        if self.recorder is not None and self.available:
            self.recorder.set_muted(speaking)
        if self.recording:
            self._set(*self._listening())

    def release(self) -> None:
        if not self.recording:
            return
        self.recording = False
        try:
            audio = self.recorder.stop()
        except Exception as exc:  # noqa: BLE001
            self._set("error", f"Microphone failed: {exc}")
            return
        if audio is None or len(audio) < MIN_SECONDS * SAMPLE_RATE:
            self._set("nothing", "Too short: hold the key while you speak.")
            return
        if self.busy:
            self._set("error", "Still working on the last question.")
            return
        self.busy = True
        rec = self.recogniser
        if rec.ready():
            self._set("transcribing", "Recognising…")
        else:
            self._set(
                "loading",
                "Loading the speech model (the first use downloads it, then it stays on this PC)…",
            )
        self.runner(lambda: rec.transcribe(audio), self._done)

    def _done(self, text, error) -> None:
        self.busy = False
        if error is not None:
            self._set("error", f"Speech recognition failed: {error}")
            return
        text = " ".join(str(text or "").split())
        if not text:
            self._set("nothing", "Didn't catch that. Hold the key and ask again.")
            return
        self._set("heard", text)
        self.transcribed.emit(text)


def build_push_to_talk(settings, parent: QObject | None = None):
    """``(PushToTalk, KeyWatcher | None)`` from settings, or ``(None, None)`` when voice is off.

    A missing extra, microphone or a bad key gives a controller in the ``off``
    state whose reason the talk button shows, rather than an error.
    """
    if not getattr(settings, "voice_enabled", False):
        return None, None
    try:
        key = parse_key(settings.voice_key)
    except ValueError as exc:
        return PushToTalk(None, None, reason=str(exc), parent=parent), None
    if not whisper_installed():
        return PushToTalk(None, None, key_label=key.label, parent=parent), None
    recorder = MicRecorder(parent)
    recogniser = WhisperRecogniser(
        settings.voice_model, Path(settings.data_dir) / "models" / "whisper"
    )
    ptt = PushToTalk(recorder, recogniser, key_label=key.label, parent=parent)
    watcher = KeyWatcher(key, global_probe(key), parent=parent)
    watcher.pressed.connect(ptt.press)
    watcher.released.connect(ptt.release)
    return ptt, watcher


# ---- python -m rtv.desktop.voice --find-button -------------------------------


def find_button(seconds: float = 20.0, out=print) -> tuple[int, int] | None:
    """Poll every game controller and print the first button pressed."""
    import time

    if sys.platform != "win32":
        out("Controller buttons are read on Windows only.")
        return None
    out(f"Press the wheel / button-box button to use (watching for {seconds:.0f} s)…")
    base = {d: joystick_buttons(d) or 0 for d in range(16)}
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for device in range(16):
            now = joystick_buttons(device)
            if now is None:
                continue
            new = now & ~base[device]
            if new:
                button = (new & -new).bit_length()
                key = f"joy{device + 1}:{button}"
                out(f"Controller {device + 1}, button {button}: RTV_VOICE_KEY={key}")
                return device + 1, button
            base[device] = now
        time.sleep(0.02)
    out("No button pressed.")
    return None


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m rtv.desktop.voice", description=__doc__)
    parser.add_argument("--find-button", action="store_true", help="print the pressed button")
    args = parser.parse_args(argv)
    if args.find_button:
        return 0 if find_button() else 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
