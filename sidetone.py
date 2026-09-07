#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sidetone.py - lokal, nollfördröjd sidoton för CW-manipulatorn.

Genereras helt lokalt (ingen nätverksväg) så operatören kan takta sitt fist
mot vad hen faktiskt trycker, oberoende av fjärrlänkens rundgångstid.

Bygger på `sounddevice` (PortAudio) och håller EN kontinuerligt öppen
ljudström (`OutputStream`) hela appens livstid. on()/off() sätter bara ett
målvärde för amplituden (0 eller 1); själva ljudblocket (`_callback`,
anropat av PortAudio i egen tråd, körs kontinuerligt oavsett om något
"hörs") glider mjukt mellan nuvarande och målamplitud över `ramp_ms`
millisekunder, sample för sample - ingen ny uppspelning startas eller
stoppas per element, så det finns ingen fil-öppning/enhets-starttid som kan
variera och ge klick eller rytmsvaj.

Historik (winsound dög inte till detta, se DOCUMENTATION.md 0.10.0):
(1) kedjade winsound.Beep()-anrop - hörbara klick mellan segmenten.
(2) SND_MEMORY|SND_ASYNC - otillåten kombination i winsound (RuntimeError).
(3) en färdigrenderad WAV-fil per element (SND_FILENAME) - mätt Python-sidig
timing var korrekt, men winsounds egen ljudstart-latens vid varje ny
filuppspelning gav ändå uppfattad svajning. En kontinuerlig ström (denna
version) har ingen sådan start-latens alls.
"""

import math
import threading

import numpy as np
import sounddevice as sd

FREQ_HZ = 600
RAMP_MS = 7          # matchar Jonnys ExpertSDR3-profil (Ramp 7ms)
SAMPLE_RATE = 44100
VOLUME = 0.35         # av max 1.0, lagom nivå i headset - default, se Sidetone.volume
BLOCKSIZE = 64        # ~1.45ms/block vid 44100Hz - PortAudios eget val (blocksize=0)
                      # gav 441 (10ms), vilket mätt gav upp till 12ms slumpmässig
                      # fördröjning mellan on()/off() och att ljudtråden märkte det


def _wasapi_output_matching(name_fragment):
    """Letar upp en WASAPI-utgång vars namn innehåller name_fragment. None om
    inget hittas eller name_fragment är tomt."""
    if not name_fragment:
        return None
    try:
        wasapi = next(h for h in sd.query_hostapis() if h["name"] == "Windows WASAPI")
        frag = name_fragment.split(" (")[0].strip()
        for i in wasapi["devices"]:
            d = sd.query_devices(i)
            if d["max_output_channels"] > 0 and frag in d["name"]:
                return i
    except Exception:
        pass
    return None


def _low_latency_output_device(preferred_name=None):
    """Väljer en låglatens-utgång (WASAPI i stället för Windows MME, typisk
    latens 80-150ms) på RÄTT fysisk enhet.

    Om `preferred_name` ges (namnet på enheten ExpertSDR3 SJÄLV spelar upp
    på, från expertsdr3_discover.local_sound_output_name() - se
    Gui.SoundCard.output i profilen) används den i första hand, så sidotonen
    hamnar på samma enhet som radion oavsett vad Windows systemstandard råkar
    stå på. Annars faller den tillbaka på att matcha Windows nuvarande
    standardutgång mot dess WASAPI-variant (gamla beteendet)."""
    dev = _wasapi_output_matching(preferred_name)
    if dev is not None:
        return dev
    try:
        default_idx = sd.default.device[1]
        default_name = sd.query_devices(default_idx)["name"]
        return _wasapi_output_matching(default_name)
    except Exception:
        return None


class Sidetone:
    def __init__(self, freq_hz=FREQ_HZ, ramp_ms=RAMP_MS, volume=VOLUME, output_name=None, log=None):
        """output_name -> se _low_latency_output_device(); None = matcha mot
        Windows systemstandard i stället för ExpertSDR3:s konfigurerade utgång.

        Om ljudenheten inte går att öppna (urdraget headset, fel sample rate
        m.m.) kraschar INTE appen - self._stream blir None och sidotonen är
        tyst tills appen startas om med enheten på plats. on()/off()/play()/
        reconfigure() är no-ops i det läget (bara self._target sätts, ingen
        ström finns som läser den)."""
        self.freq_hz = freq_hz
        self.ramp_ms = ramp_ms
        self.volume = volume
        self._log = log or (lambda _t: None)
        self._lock = threading.Lock()
        self._phase = 0.0
        self._amp = 0.0
        self._target = 0.0
        self._stream = None
        self._buf = np.empty(BLOCKSIZE, dtype=np.float32)   # återanvänd - ingen allokering per callback
        self._sr = SAMPLE_RATE
        dev = _low_latency_output_device(output_name)
        # WASAPI i delat läge kräver enhetens EGEN mix-frekvens. Anta inte 44100 -
        # prova enhetens default först, sedan de vanliga, annars tyst (PaErrorCode -9997).
        candidates = []
        try:
            candidates.append(int(round(sd.query_devices(dev)["default_samplerate"])))
        except Exception:
            pass
        for r in (48000, 44100, 96000):
            if r not in candidates:
                candidates.append(r)
        for rate in candidates:
            try:
                stream = sd.OutputStream(
                    device=dev, samplerate=rate, channels=1, dtype="float32",
                    callback=self._callback, blocksize=BLOCKSIZE, latency="low")
                stream.start()
                self._stream = stream
                self._sr = rate
                if rate != SAMPLE_RATE:
                    self._log("Sidetone: audio output opened at %d Hz" % rate)
                break
            except Exception as e:
                last_err = e
        if self._stream is None:
            self._log("Sidetone: could not open audio output (%s) - "
                       "no local CW sidetone until restart" % last_err)

    def on(self):
        with self._lock:
            self._target = 1.0

    def off(self):
        with self._lock:
            self._target = 0.0

    def play(self, duration_ms):
        """Bakåtkompatibel hjälpare: slår på nu, av efter duration_ms i en
        egen liten tråd. cw_keyer.py använder normalt on()/off() direkt och
        sköter timingen själv - den här behövs bara om något vill ha en
        "fire and forget"-ton av given längd."""
        self.on()
        threading.Timer(duration_ms / 1000.0, self.off).start()

    def reconfigure(self, freq_hz=None, ramp_ms=None, volume=None):
        if freq_hz is not None:
            self.freq_hz = freq_hz
        if ramp_ms is not None:
            self.ramp_ms = ramp_ms
        if volume is not None:
            self.volume = volume

    def stop(self):
        self.off()
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass

    def _callback(self, outdata, frames, time_info, status):
        """Vektoriserad (numpy) i stället för en Python for-loop per sample -
        körs på PortAudios realtidstråd ~690 ggr/sek (BLOCKSIZE=64 vid
        44100Hz), så per-sample Python-overhead/math.sin()-anrop var den
        i särklass hetaste koden i appen. Amplitud-rampen är monoton mot
        target inom EN callback (target ändras bara av on()/off(), lästa en
        gång i callbackens start), så hela blockets amp-kurva kan räknas ut
        som en vektor i stället för sample-för-sample."""
        with self._lock:
            target = self._target
            amp = self._amp
        ramp_n = max(1, int(self._sr * self.ramp_ms / 1000))
        step = 1.0 / ramp_n
        phase = self._phase
        two_pi_f = 2 * math.pi * self.freq_hz / self._sr

        k = np.arange(frames, dtype=np.float64)
        if target > amp:
            amp_arr = np.minimum(target, amp + step * (k + 1))
        elif target < amp:
            amp_arr = np.maximum(target, amp - step * (k + 1))
        else:
            amp_arr = np.full(frames, amp, dtype=np.float64)
        new_amp = float(amp_arr[-1]) if frames else amp

        phase_arr = phase + two_pi_f * k
        new_phase = (phase + two_pi_f * frames) % (2 * math.pi)

        buf = self._buf if frames == BLOCKSIZE else np.empty(frames, dtype=np.float32)
        np.multiply(amp_arr * self.volume, np.sin(phase_arr), out=buf, casting="unsafe")
        outdata[:, 0] = buf

        with self._lock:
            self._amp = new_amp
            self._phase = new_phase
