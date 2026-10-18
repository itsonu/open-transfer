"""Original soundtrack + effects for the demo video (see ``build.py``).

120 BPM in C major (C-Am-F-G, then it resolves on C under the outro card).
The effects are in the same key and room as the music and land on the times
``record.py`` measured for each beat, so they sit inside the track.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 48_000
BEAT = 0.5
rng = np.random.default_rng(7)


def hz(note: str) -> float:
    names = {
        "C": 0,
        "C#": 1,
        "D": 2,
        "D#": 3,
        "E": 4,
        "F": 5,
        "F#": 6,
        "G": 7,
        "G#": 8,
        "A": 9,
        "A#": 10,
        "B": 11,
    }
    name, octave = note[:-1], int(note[-1])
    return 440.0 * 2 ** ((names[name] + 12 * (octave + 1) - 69) / 12)


def place(buf: np.ndarray, sig: np.ndarray, t: float, gain: float = 1.0, pan: float = 0.0) -> None:
    i = int(t * SR)
    if i >= len(buf):
        return
    sig = sig[: len(buf) - i]
    left, right = np.cos((pan + 1) * np.pi / 4), np.sin((pan + 1) * np.pi / 4)
    buf[i : i + len(sig), 0] += sig * gain * left * 1.414
    buf[i : i + len(sig), 1] += sig * gain * right * 1.414


def env(n: int, a: float, d: float) -> np.ndarray:
    t = np.arange(n) / SR
    return np.minimum(1.0, t / max(a, 1e-4)) * np.exp(-t / d)


def onepole_lp(x: np.ndarray, cutoff: float) -> np.ndarray:
    a = np.exp(-2 * np.pi * cutoff / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):  # small signals only
        acc = (1 - a) * v + a * acc
        y[i] = acc
    return y


# ---------- instruments ----------
def kick(gain=1.0):
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    f = 58 + 110 * np.exp(-t / 0.03)
    ph = 2 * np.pi * np.cumsum(f) / SR
    click_ = np.sin(2 * np.pi * 1800 * t) * np.exp(-t / 0.004) * 0.25
    return (np.sin(ph) * np.exp(-t / 0.09) + click_) * gain


def hat(gain=1.0):
    n = int(0.06 * SR)
    x = rng.standard_normal(n)
    x = x - onepole_lp(x, 6000)  # high-pass-ish
    return x * env(n, 0.0005, 0.018) * gain


def clap(gain=1.0):
    n = int(0.25 * SR)
    x = rng.standard_normal(n)
    x = onepole_lp(x, 3500) - onepole_lp(x, 900)
    e = np.zeros(n)
    for off in (0.0, 0.011, 0.022):
        e += env(n, 0.0005, 0.03) * (np.arange(n) >= off * SR)
    e[int(0.03 * SR) :] = np.exp(-(np.arange(n - int(0.03 * SR)) / SR) / 0.07) * 0.8
    return x * e * gain


def pluck(freq, dur=0.6, gain=1.0):
    n = int(dur * SR)
    t = np.arange(n) / SR
    s = sum(
        np.sin(2 * np.pi * freq * k * t) * (0.55 ** (k - 1)) * np.exp(-t * (6 + 3 * k))
        for k in range(1, 5)
    )
    return s * np.minimum(1, t / 0.003) * gain


def pad(freqs, dur, gain=1.0):
    n = int(dur * SR)
    t = np.arange(n) / SR
    s = np.zeros(n)
    for f in freqs:
        for det in (-0.12, 0.0, 0.12):
            ff = f * 2 ** (det / 12)
            for k in range(1, 7):
                s += np.sin(2 * np.pi * ff * k * t + rng.uniform(0, 6.28)) / (k**1.6)
    attack = np.minimum(1, t / 0.25)
    release = np.minimum(1, (dur - t) / 0.35)
    return s / (len(freqs) * 9) * attack * np.clip(release, 0, 1) * gain


def bass(freq, dur, gain=1.0):
    n = int(dur * SR)
    t = np.arange(n) / SR
    s = sum(np.sin(2 * np.pi * freq * k * t) / k**1.3 for k in range(1, 7))
    s = s * 0.75
    return s * np.minimum(1, t / 0.01) * np.clip((dur - t) / 0.05, 0, 1) * np.exp(-t / 0.9) * gain


def bell(freq, dur=1.6, gain=1.0):
    n = int(dur * SR)
    t = np.arange(n) / SR
    s = (
        np.sin(2 * np.pi * freq * t) * np.exp(-t / 0.9)
        + 0.4 * np.sin(2 * np.pi * freq * 2.0 * t) * np.exp(-t / 0.4)
        + 0.18 * np.sin(2 * np.pi * freq * 3.01 * t) * np.exp(-t / 0.18)
    )
    return s * np.minimum(1, t / 0.002) * gain


def pop(freq, gain=1.0):
    n = int(0.16 * SR)
    t = np.arange(n) / SR
    f = freq * (1 + 0.6 * np.exp(-t / 0.012))
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, 0.001, 0.045) * gain


def click(gain=1.0):
    n = int(0.05 * SR)
    t = np.arange(n) / SR
    x = rng.standard_normal(n)
    x = onepole_lp(x, 4000) - onepole_lp(x, 1500)
    return (x * 0.5 + np.sin(2 * np.pi * hz("E6") * t)) * env(n, 0.0005, 0.010) * gain


def whoosh(dur, up=True, gain=1.0):
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = rng.standard_normal(n)
    cut = np.linspace(400, 5000, n) if up else np.linspace(5000, 400, n)
    y = np.empty(n)
    acc = 0.0
    for i in range(n):
        a = np.exp(-2 * np.pi * cut[i] / SR)
        acc = (1 - a) * x[i] + a * acc
        y[i] = acc
    shape = np.sin(np.pi * t / dur) ** 2
    return y * shape * gain


def reverb(x: np.ndarray, seconds=1.3, wet=0.18) -> np.ndarray:
    n = int(seconds * SR)
    t = np.arange(n) / SR
    out = np.empty_like(x)
    for ch in range(2):
        ir = rng.standard_normal(n) * np.exp(-t / 0.35)
        ir[0] = 0
        ir /= np.sqrt((ir**2).sum())
        size = 1 << int(np.ceil(np.log2(len(x) + n)))
        y = np.fft.irfft(np.fft.rfft(x[:, ch], size) * np.fft.rfft(ir, size), size)[: len(x)]
        out[:, ch] = x[:, ch] + wet * y
    return out


def render(events: dict[str, float], duration: float, out: Path) -> None:
    n_samples = int(SR * duration)
    music = np.zeros((n_samples, 2))
    sfx = np.zeros((n_samples, 2))

    CHORDS = {  # bar -> (root, chord tones)
        "C": ("C3", ["C4", "E4", "G4"]),
        "Am": ("A2", ["A3", "C4", "E4"]),
        "F": ("F2", ["F3", "A3", "C4"]),
        "G": ("G2", ["G3", "B3", "D4"]),
    }
    prog = ["C", "Am", "F", "G", "C", "Am", "F", "G"]  # 8 bars = 0-16 s
    for bar, name in enumerate(prog):
        t0 = bar * 2.0
        root, tones = CHORDS[name]
        place(music, pad([hz(n) for n in tones], 2.05, 0.30), t0)
        for b in range(4):
            place(music, bass(hz(root), 0.48, 0.40 if bar else 0.28), t0 + b * BEAT)
        arp = [*tones, tones[0][:-1] + str(int(tones[0][-1]) + 1)]  # + the root an octave up
        for s in range(8):
            note = arp[[0, 1, 2, 3, 2, 1, 2, 3][s]]
            place(
                music,
                pluck(hz(note) * 2, 0.5, 0.15 if bar else 0.11),
                t0 + s * 0.25,
                pan=-0.35 if s % 2 else 0.35,
            )
    # 16 s on: resolve on C and ring out under the outro card
    END = duration
    place(music, pad([hz("C4"), hz("E4"), hz("G4"), hz("C5")], END - 16.0, 0.44), 16.0)
    place(music, pad([hz("G4"), hz("C5"), hz("E5")], END - 16.0, 0.15), 16.0)
    for b in range(int((END - 16.0 - 1.0) / BEAT)):
        place(music, bass(hz("C3"), 0.48, 0.36 * (1 - b / 14)), 16.0 + b * BEAT)
    for s, n in enumerate(["C5", "E5", "G5", "C6", "G5", "E5", "C5"]):
        place(
            music,
            pluck(hz(n), 0.9, 0.16 * (1 - s / 12)),
            16.0 + s * 0.5,
            pan=0.3 if s % 2 else -0.3,
        )

    # drums: in after the hook, thin out under the outro
    for i in range(int(16.0 / BEAT)):
        t = i * BEAT
        place(music, kick(0.50 if t >= 3.0 else 0.30), t)
        if t >= 3.0:
            place(music, hat(0.14), t + 0.25, pan=0.25)
            if i % 2 == 1:
                place(music, clap(0.15), t, pan=-0.1)
    for k in range(4):  # little roll into the outro
        place(music, clap(0.05 + 0.025 * k), 15.5 + k * 0.125)
    for i in range(int(16.0 / BEAT), int((END - 1.5) / BEAT)):
        t = i * BEAT
        place(music, kick(0.36 if t < 18.0 else 0.24), t)
        place(music, hat(0.09), t + 0.25, pan=0.25)

    # ---------- SFX on the real event times (in key, under the music) ----------
    for i, n in enumerate(["C5", "E5", "G5", "C6"]):  # the four device tiles land
        place(sfx, pop(hz(n), 0.20), 0.40 + i * 0.18, pan=[-0.3, 0.3, -0.3, 0.3][i])
    place(sfx, bell(hz("C6"), 1.6, 0.10), 0.05)
    place(sfx, whoosh(0.5, True, 0.09), events["hook-out"])
    place(sfx, whoosh(0.8, True, 0.07), events["fly"])  # file chip flies in
    place(sfx, pop(hz("G5"), 0.16), events["drop"])  # lands in the drop zone
    for key, n in (("pick1", "E6"), ("pick2", "G6")):
        place(sfx, click(0.18), events[key])
        place(sfx, pop(hz(n), 0.07), events[key] + 0.02)
    place(sfx, click(0.20), events["send"])
    place(sfx, whoosh(0.4, True, 0.06), events["send"] + 0.05)
    place(sfx, bell(hz("G5"), 1.2, 0.10), events["incoming"] + 0.05)  # the prompt on the phone
    place(sfx, bell(hz("C6"), 1.2, 0.09), events["incoming"] + 0.17)
    place(sfx, click(0.16), events["accept"])
    for i, n in enumerate(["C6", "E6", "G6"]):  # both devices: Delivered
        place(sfx, bell(hz(n), 1.4, 0.11), events["delivered"] + i * 0.12)
    place(sfx, whoosh(0.4, False, 0.06), events["outro"])
    place(sfx, bell(hz("C6"), 3.0, 0.15), events["outro"] + 0.45)
    place(sfx, bell(hz("E6"), 3.0, 0.06), events["outro"] + 0.48)

    mix = reverb(music, wet=0.16) + reverb(sfx, wet=0.30)
    for ch in range(2):
        mix[:, ch] = mix[:, ch] - onepole_lp(mix[:, ch], 35)
        mix[:, ch] -= mix[:, ch].mean()
    # tail fade on the last 0.8 s
    tail = np.clip((duration - np.arange(n_samples) / SR) / 0.6, 0, 1)[:, None]
    mix *= tail
    mix = np.tanh(mix * 1.1) / np.tanh(1.1)  # gentle soft-clip glue
    mix *= 0.89 / np.max(np.abs(mix))  # peak ≈ -1 dBFS
    pcm = (mix * 32767).astype("<i2")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
