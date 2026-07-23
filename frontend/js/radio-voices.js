/**
 * One voice per role, so the pitwall is legible by ear alone.
 *
 * This is the *offline* copy of `rtv.pitwall.tts.DEFAULT_VOICES`: the page asks
 * `/api/v1/pitwall/tts` for the live table at boot and uses this only if that
 * call fails or the backend is not mounted. The two are kept identical by
 * `tests/test_pitwall_audio_frontend.py`, which parses this literal -- which is
 * why it is strict JSON with no expressions in it.
 *
 * `browser_hints` are matched (case-insensitively, as substrings) against
 * `speechSynthesis.getVoices()`; `rate` and `pitch` apply on every engine, so
 * two roles that land on the same underlying voice still sound different.
 */

export const DEFAULT_VOICES = {
  "strategist": {
    "voice": "onyx",
    "lang": "en-GB",
    "rate": 0.98,
    "pitch": 0.85,
    "browser_hints": ["George", "Daniel", "Google UK English Male", "en-GB"],
    "label": "Strategist"
  },
  "vehicle_engineer": {
    "voice": "echo",
    "lang": "en-GB",
    "rate": 1.02,
    "pitch": 1.0,
    "browser_hints": ["Ryan", "Arthur", "Google UK English Male", "en-GB"],
    "label": "Engineer"
  },
  "spotter": {
    "voice": "fable",
    "lang": "en-US",
    "rate": 1.22,
    "pitch": 1.18,
    "browser_hints": ["Guy", "Christopher", "Google US English", "en-US"],
    "label": "Spotter"
  },
  "coach": {
    "voice": "nova",
    "lang": "en-GB",
    "rate": 0.95,
    "pitch": 1.1,
    "browser_hints": ["Sonia", "Libby", "Google UK English Female", "en-GB"],
    "label": "Coach"
  },
  "driver": {
    "voice": "alloy",
    "lang": "en-GB",
    "rate": 1.0,
    "pitch": 1.0,
    "browser_hints": [],
    "label": "Driver"
  },
  "default": {
    "voice": "alloy",
    "lang": "en-GB",
    "rate": 1.0,
    "pitch": 1.0,
    "browser_hints": ["en-GB", "en-US"],
    "label": "Pitwall"
  }
};

export default DEFAULT_VOICES;
