"""Speak the daily push — the same voice 爸菲特 uses, in the same group chat.

Peter's ask (2026-09-11): every morning and afternoon push should be followed by a
voice bubble, in the voice already used by @Hermes_Investment_Strategy_bot — the
same bot that sends these reports.

THE VOICE IS NOT REIMPLEMENTED HERE. It is the module that bot already uses
(`botffet_voice.py`, zh-TW-YunJheNeural at -5% / -8Hz, a setting Peter auditioned
and chose on 2026-08-29). Copying it would mean two voices drifting apart the
moment one is retuned — the same trap the report and the dashboard fell into.
So this locates that module and calls it. If it cannot be found, voice is skipped
with a loud reason and the text push is completely unaffected.

Nothing in here may break a report. The text always goes out first, and every
failure below is caught and logged.
"""

import io
import os
import sys
import datetime

import requests

# Where 爸菲特's voice module lives. FRB_VOICE_MODULE_DIR overrides, for when the
# repo moves — which is why the failure below names the path it looked in.
_CANDIDATE_DIRS = [
    os.getenv('FRB_VOICE_MODULE_DIR'),
    os.path.join(os.path.expanduser('~'), 'GitHub', 'Investment_Strategy_Research_2026', 'scripts'),
]

# Full report, not the 1500-char default: a whole cleaned push is ~2,600 chars in
# the morning and ~3,500 at the close, both inside edge-tts's own 4,000 ceiling.
# Cutting it would have the bubble stop mid-portfolio.
DEFAULT_MAX_CHARS = 4000


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _load_voice_module():
    """The shared botffet_voice module, or (None, reason)."""
    for d in _CANDIDATE_DIRS:
        if not d or not os.path.isdir(d):
            continue
        if d not in sys.path:
            sys.path.insert(0, d)
        try:
            import botffet_voice
            return botffet_voice, None
        except Exception as exc:                                   # noqa: BLE001
            return None, f'found {d} but could not import botffet_voice: {exc}'
    looked = ' / '.join(d for d in _CANDIDATE_DIRS if d) or '(nowhere)'
    return None, f"botffet_voice.py not found (looked in: {looked})"


def for_speech(text):
    """The listenable slice of a push: the date line, the market overview, the
    portfolio's daily and total P&L, and the AI analyst's summary.

    Peter's choice on 2026-09-11, given the measured alternatives. The per-stock
    lines are deliberately left out: reading 14 holdings' open, close, change,
    percentage and volume aloud ran to 9 minutes in the morning and 12 at the
    close, and every one of those figures is already on screen in the message
    directly above the bubble. This runs about 4 minutes and 2½.

    A staleness warning IS spoken — a listener who cannot see the header still
    needs to know the numbers are not today's. Falls back to the whole report if
    the summary section is absent (AI failed, or the section is switched off), so
    the bubble is never silently empty.
    """
    lines = (text or '').splitlines()
    out = []

    for i, l in enumerate(lines):
        if l.startswith('📊'):
            out.append(l)
            nxt = lines[i + 1] if i + 1 < len(lines) else ''
            if nxt.startswith('⚠️') and '尚未發布' in nxt:
                out.append(nxt)
            break

    for i, l in enumerate(lines):
        if l.startswith('市場總覽'):
            out.append(l)
            for nxt in lines[i + 1:]:
                if not nxt.strip():
                    break
                out.append(nxt)
            break

    for i, l in enumerate(lines):
        if l.startswith('💰'):
            out.append(l)
            for nxt in lines[i + 1:]:          # ⚠️ partial-total / 昨收 mismatch notes
                if not nxt.startswith('⚠️'):
                    break
                out.append(nxt)
            break

    j = next((i for i, l in enumerate(lines) if l.startswith('📋')), None)
    if j is None:
        return text                            # no summary to anchor on — read it all
    out.extend(lines[j:])

    return '\n'.join(out).strip() or text


def render(text, cfg=None):
    """The report spoken, as Opus-in-Ogg bytes ready for sendVoice. None if voice
    is off or unavailable — never raises."""
    vcfg = ((cfg or {}).get('voice') or {})
    if not vcfg.get('enabled', True):
        print(f"[{_now()}] Voice disabled in bot_config.")
        return None

    mod, reason = _load_voice_module()
    if mod is None:
        print(f"[{_now()}] Voice skipped: {reason}")
        return None

    try:
        settings = mod.settings()
        # Its voice/rate/pitch are the point — only the length is ours, and only
        # because 爸菲特's own replies are far shorter than a whole report.
        settings['max_chars'] = int(vcfg.get('max_chars', DEFAULT_MAX_CHARS))
        spoken = mod.speakable(for_speech(text), settings['max_chars'])
        if not spoken:
            print(f"[{_now()}] Voice skipped: nothing listenable in the report.")
            return None
        data = mod.synthesize(spoken, settings)
        print(f"[{_now()}] Voice rendered: {len(spoken)} chars → {len(data):,} bytes "
              f"({settings['voice']} {settings['rate']} {settings['pitch']}).")
        return data
    except Exception as exc:                                       # noqa: BLE001
        print(f"[{_now()}] Voice skipped: {exc}")
        return None


def send(token, chat_id, data, caption=None):
    """POST the bubble to Telegram. Returns bool; never raises."""
    if not (token and chat_id and data):
        return False
    payload = {'chat_id': str(chat_id)}
    if caption:
        payload['caption'] = caption[:1024]
    try:
        resp = requests.post(
            f'https://api.telegram.org/bot{token}/sendVoice',
            data=payload,
            files={'voice': ('report.ogg', io.BytesIO(data), 'audio/ogg')},
            timeout=120,          # the upload, not the synthesis, is the slow leg here
        )
        if resp.ok:
            print(f"[{_now()}] Voice sent → {chat_id}.")
            return True
        print(f"[{_now()}] Voice send failed ({chat_id}): {resp.text[:300]}")
    except Exception as exc:                                       # noqa: BLE001
        print(f"[{_now()}] Voice send error ({chat_id}): {exc}")
    return False
