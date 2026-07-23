/**
 * The noisy half: everything the pure audio manager deliberately does not know.
 *
 * Three engines, tried in that order for each message:
 *
 *   1. BACKEND AUDIO -- if the message carries an `audio_url`, a backend TTS
 *      provider is configured and its bytes win. One <audio> element, reused.
 *   2. WEB SPEECH -- the default. `speechSynthesis`, zero cost, offline, and the
 *      only engine this project assumes exists.
 *   3. NOTHING -- a browser with neither. The utterance ends immediately so the
 *      queue keeps draining, the UI says so once, and no exception is thrown.
 *      A silent radio is a bad day; a jammed queue is a broken app.
 *
 * Voices are assigned per role, so the strategist and the spotter are
 * distinguishable by ear. Where the browser exposes named voices we match the
 * backend's hints against them; where it does not, roles are spread across
 * whatever voices exist and *still* differ by rate and pitch, which every engine
 * supports. The radio-click cue ahead of a critical call is synthesised with
 * WebAudio -- no asset files, nothing to 404.
 */

import { DEFAULT_VOICES } from './radio-voices.js';

/** A short transmit click, built from oscillators. No files, no fetches. */
export class RadioCue {
  constructor() {
    this._ctx = null;
    this.supported =
      typeof window !== 'undefined' &&
      !!(window.AudioContext || window.webkitAudioContext);
  }

  /** Browsers only allow audio after a gesture; call this from a click handler. */
  prime() {
    const ctx = this._context();
    if (ctx && ctx.state === 'suspended') ctx.resume().catch(() => {});
    return !!ctx;
  }

  _context() {
    if (!this.supported) return null;
    if (!this._ctx) {
      try {
        const Ctor = window.AudioContext || window.webkitAudioContext;
        this._ctx = new Ctor();
      } catch (_) {
        this.supported = false;
        return null;
      }
    }
    return this._ctx;
  }

  play(volume = 1) {
    const ctx = this._context();
    if (!ctx) return false;
    try {
      if (ctx.state === 'suspended') ctx.resume().catch(() => {});
      const t0 = ctx.currentTime;
      // Two clipped blips a beat apart: the sound of a transmit key, not a beep.
      [
        { f: 1180, at: 0, len: 0.035 },
        { f: 820, at: 0.055, len: 0.045 },
      ].forEach(({ f, at, len }) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'square';
        osc.frequency.setValueAtTime(f, t0 + at);
        gain.gain.setValueAtTime(0.0001, t0 + at);
        gain.gain.exponentialRampToValueAtTime(0.09 * volume + 0.0001, t0 + at + 0.006);
        gain.gain.exponentialRampToValueAtTime(0.0001, t0 + at + len);
        osc.connect(gain).connect(ctx.destination);
        osc.start(t0 + at);
        osc.stop(t0 + at + len + 0.01);
      });
      return true;
    } catch (_) {
      return false;
    }
  }
}

/**
 * Speaks one message at a time. The audio manager decides *what*; this decides
 * *how*, and calls `onEnd(message_id)` when the channel is free again.
 */
export class RadioSpeaker {
  constructor(options = {}) {
    const { voices = DEFAULT_VOICES, onEnd = () => {}, onNotice = () => {} } = options;

    this.voices = voices;
    this.onEnd = onEnd;
    this.onNotice = onNotice;
    this.cue = new RadioCue();

    this.synth =
      typeof window !== 'undefined' && 'speechSynthesis' in window
        ? window.speechSynthesis
        : null;
    this.hasWebSpeech = !!this.synth && typeof window.SpeechSynthesisUtterance === 'function';
    this._audio = typeof Audio === 'function' ? new Audio() : null;
    this._voiceCache = new Map();
    this._current = null;
    this._keepalive = null;
    this._noticed = false;

    if (this.hasWebSpeech && typeof this.synth.addEventListener === 'function') {
      // Voice lists load asynchronously in most browsers; drop the cache when
      // they arrive so the second call gets the right voice.
      this.synth.addEventListener('voiceschanged', () => this._voiceCache.clear());
    }
  }

  get supported() {
    return this.hasWebSpeech || !!this._audio;
  }

  /** Where the engine stands, for the UI to show honestly. */
  describe() {
    return {
      webSpeech: this.hasWebSpeech,
      backendAudio: !!this._audio,
      cue: this.cue.supported,
      voices: this.hasWebSpeech ? (this.synth.getVoices() || []).length : 0,
    };
  }

  /** Must be called from a user gesture once, or nothing will make a sound. */
  prime() {
    this.cue.prime();
    if (this._audio) {
      // A muted no-op play unlocks the element on mobile Safari.
      this._audio.muted = true;
      const attempt = this._audio.play();
      if (attempt && typeof attempt.catch === 'function') attempt.catch(() => {});
      this._audio.pause();
      this._audio.muted = false;
    }
  }

  speak(message, opts = {}) {
    const { volume = 1, cue = false } = opts;
    this._current = message.message_id;
    if (cue) this.cue.play(volume);

    if (message.audio_url && this._audio) {
      if (this._playBackend(message, volume)) return;
    }
    if (this.hasWebSpeech) {
      if (this._playWebSpeech(message, volume)) return;
    }
    // Nothing can speak. End it immediately so the queue keeps moving, and say
    // so exactly once rather than throwing on every message.
    if (!this._noticed) {
      this._noticed = true;
      this.onNotice(
        'This browser has no speech engine, so the pitwall is text-only. ' +
          'Radio still arrives; it just is not spoken.'
      );
    }
    this._finish(message.message_id);
  }

  cancel() {
    const id = this._current;
    this._current = null;
    this._stopKeepalive();
    if (this._audio) {
      try {
        this._audio.pause();
        this._audio.removeAttribute('src');
      } catch (_) {
        /* an element that was never played has nothing to stop */
      }
    }
    if (this.hasWebSpeech) {
      try {
        this.synth.cancel();
      } catch (_) {
        /* cancelling an idle synth is not an error worth surfacing */
      }
    }
    return id;
  }

  // ---- engines ---------------------------------------------------------
  _playBackend(message, volume) {
    try {
      const audio = this._audio;
      audio.pause();
      audio.src = message.audio_url;
      audio.volume = volume;
      audio.onended = () => this._finish(message.message_id);
      audio.onerror = () => {
        // The vendor is down or the clip 404'd. Say it in the browser's voice
        // rather than dropping the call: a missed radio message is the failure
        // that matters, a different voice is not.
        if (this._current !== message.message_id) return;
        if (this.hasWebSpeech && this._playWebSpeech(message, volume)) return;
        this._finish(message.message_id);
      };
      const attempt = audio.play();
      if (attempt && typeof attempt.catch === 'function') {
        attempt.catch(() => {
          if (this._current !== message.message_id) return;
          if (!(this.hasWebSpeech && this._playWebSpeech(message, volume))) {
            this._finish(message.message_id);
          }
        });
      }
      return true;
    } catch (_) {
      return false;
    }
  }

  _playWebSpeech(message, volume) {
    try {
      const profile = this.voiceProfile(message.agent);
      const utterance = new window.SpeechSynthesisUtterance(message.spoken_text);
      utterance.volume = volume;
      utterance.rate = profile.rate;
      utterance.pitch = profile.pitch;
      utterance.lang = profile.lang;
      const voice = this._browserVoice(message.agent, profile);
      if (voice) utterance.voice = voice;
      utterance.onend = () => this._finish(message.message_id);
      utterance.onerror = () => this._finish(message.message_id);
      this.synth.cancel();
      // Chrome drops an utterance queued in the same tick as a cancel(), which
      // is *exactly* the interrupt path a critical call takes. One frame of
      // delay is inaudible and makes the interrupt reliable; the guard means a
      // second interrupt inside that frame still wins.
      setTimeout(() => {
        if (this._current !== message.message_id) return;
        try {
          this.synth.speak(utterance);
          this._startKeepalive();
        } catch (_) {
          this._finish(message.message_id);
        }
      }, 40);
      return true;
    } catch (_) {
      return false;
    }
  }

  _finish(id) {
    this._stopKeepalive();
    if (this._current !== id) return;
    this._current = null;
    this.onEnd(id);
  }

  // Chrome stops speaking after ~15 s unless nudged. Cheap insurance.
  _startKeepalive() {
    this._stopKeepalive();
    if (typeof setInterval !== 'function') return;
    this._keepalive = setInterval(() => {
      if (!this.synth || !this.synth.speaking) return this._stopKeepalive();
      try {
        this.synth.pause();
        this.synth.resume();
      } catch (_) {
        this._stopKeepalive();
      }
    }, 5000);
  }

  _stopKeepalive() {
    if (this._keepalive) {
      clearInterval(this._keepalive);
      this._keepalive = null;
    }
  }

  // ---- voices ----------------------------------------------------------
  /** Swap the table (the backend's, once `/pitwall/tts` answers) and re-resolve. */
  setVoices(voices) {
    this.voices = voices;
    this._voiceCache.clear();
  }

  voiceProfile(agent) {
    return this.voices[agent] || this.voices.default || DEFAULT_VOICES.default;
  }

  /**
   * Resolve a role to a concrete browser voice.
   *
   * Hints first (the backend names real voices where it can), then language,
   * then a deterministic spread across whatever the browser has, so two roles
   * do not collapse onto one voice just because nothing matched.
   */
  _browserVoice(agent, profile) {
    if (!this.hasWebSpeech) return null;
    if (this._voiceCache.has(agent)) return this._voiceCache.get(agent);
    const available = this.synth.getVoices() || [];
    if (!available.length) return null;

    let chosen = null;
    for (const hint of profile.browser_hints || []) {
      const needle = String(hint).toLowerCase();
      chosen = available.find(
        (v) =>
          String(v.name).toLowerCase().includes(needle) ||
          String(v.lang).toLowerCase().includes(needle)
      );
      if (chosen) break;
    }
    if (!chosen) {
      const english = available.filter((v) => String(v.lang).toLowerCase().startsWith('en'));
      const pool = english.length ? english : available;
      const roles = Object.keys(this.voices);
      const index = Math.max(0, roles.indexOf(agent));
      chosen = pool[index % pool.length];
    }
    this._voiceCache.set(agent, chosen || null);
    return chosen || null;
  }
}

/**
 * Push-to-talk. Feature-detected: `supported` is false on every browser without
 * the Web Speech recognition API, and the caller hides the button rather than
 * showing one that cannot work.
 */
export class PushToTalk {
  constructor(options = {}) {
    const { onResult = () => {}, onState = () => {}, lang = 'en-GB' } = options;
    const Ctor =
      typeof window !== 'undefined'
        ? window.SpeechRecognition || window.webkitSpeechRecognition
        : null;
    this.supported = !!Ctor;
    this.listening = false;
    this._onResult = onResult;
    this._onState = onState;
    this._recognition = null;
    if (!this.supported) return;

    const recognition = new Ctor();
    recognition.lang = lang;
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;
    recognition.onresult = (event) => {
      const result = event.results && event.results[0] && event.results[0][0];
      const text = result ? String(result.transcript || '').trim() : '';
      if (text) this._onResult(text, result.confidence ?? null);
    };
    recognition.onend = () => {
      this.listening = false;
      this._onState('idle');
    };
    recognition.onerror = (event) => {
      this.listening = false;
      this._onState('error', (event && event.error) || 'unknown');
    };
    this._recognition = recognition;
  }

  start() {
    if (!this.supported || this.listening) return false;
    try {
      this._recognition.start();
      this.listening = true;
      this._onState('listening');
      return true;
    } catch (_) {
      this.listening = false;
      return false;
    }
  }

  stop() {
    if (!this.supported || !this.listening) return false;
    try {
      this._recognition.stop();
    } catch (_) {
      this.listening = false;
    }
    return true;
  }
}
