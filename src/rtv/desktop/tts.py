"""The radio's voice: QTextToSpeech (Windows WinRT/SAPI) behind the audio rules.

``QtSpeaker`` is the speaker ``RadioAudioManager`` drives: it picks a voice per
agent from ``/pitwall/tts`` (the same per-agent lang/rate/pitch the web radio
uses), and tells the manager when an utterance ends. With no speech engine the
radio is shown, not spoken, and the window says so.
"""

from __future__ import annotations

from PySide6.QtCore import QLocale, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from .model.audio_queue import RadioAudioManager

NO_ENGINE = "No speech engine on this system: the radio is shown, not spoken."


class QtSpeaker(QObject):
    """Speaks one message at a time and reports when it is done."""

    finished = Signal(str)

    def __init__(self, engine: str | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.voices: dict = {}
        self.available = False
        self.reason = NO_ENGINE
        self._current: str | None = None
        self._tts = None
        try:
            from PySide6.QtTextToSpeech import QTextToSpeech

            engines = QTextToSpeech.availableEngines()
            real = [e for e in engines if e != "mock"]
            if engine is None:
                engine = "winrt" if "winrt" in real else (real[0] if real else None)
            if engine:
                self._tts = QTextToSpeech(engine, self)
                self._tts.stateChanged.connect(self._state)
                self.available = True
                self.reason = ""
        except Exception:  # noqa: BLE001 - no speech module means a silent radio
            self._tts = None

    def set_voices(self, voices: dict) -> None:
        self.voices = voices or {}

    def _voice_for(self, agent: str):
        spec = self.voices.get(agent) or {}
        if not self._tts:
            return None, spec
        hints = [h.lower() for h in spec.get("browser_hints") or []]
        lang = (spec.get("lang") or "").replace("-", "_")
        candidates = self._tts.availableVoices()
        if lang:
            locale = QLocale(lang)
            for voice in self._tts.findVoices(locale.language(), locale.territory()):
                if voice not in candidates:
                    candidates.append(voice)
        for hint in hints:
            for voice in candidates:
                if hint and hint in voice.name().lower():
                    return voice, spec
        if lang:
            locale = QLocale(lang)
            for voice in candidates:
                if voice.locale().language() == locale.language():
                    return voice, spec
        return None, spec

    def speak(self, message: dict, volume: float = 1.0, cue: bool = False) -> None:
        self._current = message.get("message_id")
        if not self._tts:
            # Nothing to say it with: finish on the next turn of the event loop so
            # the queue still drains without re-entering the manager.
            done = self._current or ""
            QTimer.singleShot(0, lambda: self.finished.emit(done))
            return
        voice, spec = self._voice_for(message.get("agent") or "")
        if voice is not None:
            self._tts.setVoice(voice)
        rate = float(spec.get("rate") or 1.0)
        pitch = float(spec.get("pitch") or 1.0)
        # Web Speech rate/pitch centre on 1.0; Qt's centre on 0.0 within [-1, 1].
        self._tts.setRate(max(-1.0, min(1.0, rate - 1.0)))
        self._tts.setPitch(max(-1.0, min(1.0, pitch - 1.0)))
        self._tts.setVolume(max(0.0, min(1.0, volume)))
        if cue:
            QApplication.beep()  # the transmit click ahead of a critical call
        self._tts.say(message.get("spoken_text") or "")

    def cancel(self) -> None:
        self._current = None
        if self._tts:
            self._tts.stop()

    def _state(self, state) -> None:
        from PySide6.QtTextToSpeech import QTextToSpeech

        if state in (QTextToSpeech.State.Ready, QTextToSpeech.State.Error) and self._current:
            done, self._current = self._current, None
            self.finished.emit(done)


def make_radio(speaker: QtSpeaker, on_change) -> RadioAudioManager:
    manager = RadioAudioManager(speaker=speaker, on_change=on_change, volume=0.9)
    speaker.finished.connect(manager.finished)
    return manager
