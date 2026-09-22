"""Compose the score for the film.

    python tools/compose_score.py 89.3 --out bed.wav

Then marry it to the picture (any ffmpeg with libx264 and aac; `pip install
imageio-ffmpeg` provides one):

    ffmpeg -ss 5.6 -i film/raw/*.webm -i bed.wav -map 0:v:0 -map 1:a:0 -shortest \
      -c:v libx264 -preset slow -crf 18 -pix_fmt yuv420p \
      -c:a aac -b:a 192k -movflags +faststart bmail-film.mp4

The -ss trims the browser starting up, which the capture includes and the film
does not want.

Written rather than sourced: a synthesised bed carries no licence and no
attribution, and it can be cut to the exact length of the picture.

It is deliberately plain — a slow four-chord loop in A minor, a warm pad, a
sparse bell figure and a quiet sub. Under a product film the music's whole job
is to stop the silence feeling like a fault; anything with an opinion competes
with the thing being demonstrated.
"""
import argparse
import wave

import numpy as np

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("seconds", type=float, help="length of the picture it sits under")
ap.add_argument("--out", default="bed.wav")
ap.add_argument("--peak", type=float, default=-10.0,
                help="dBFS. Quiet enough to leave room for a voiceover later.")
args = ap.parse_args()

SR = 48_000
SECONDS = args.seconds
OUT = args.out

BPM = 68
BAR = 4 * 60 / BPM                     # 3.53s
N = int(SECONDS * SR)
t = np.arange(N) / SR

# A minor, voiced open and high enough to stay out of the way of speech.
def hz(semitones_from_a3):
    return 220.0 * 2 ** (semitones_from_a3 / 12)

PROG = [                               # one chord per bar, four-bar loop
    [0, 3, 7, 12],                     # Am
    [-4, 0, 5, 12],                    # F
    [-9, 3, 7, 15],                    # C
    [-2, 2, 7, 14],                    # G
]


def envelope(start, length, attack, release):
    """A slow swell. Shaped with a raised cosine so there is no click at either
    end and no audible corner in the middle."""
    env = np.zeros(N)
    a, b = int(start * SR), int((start + length) * SR)
    if a >= N:
        return env
    b = min(b, N)
    span = b - a
    up = int(min(attack * SR, span // 2))
    down = int(min(release * SR, span - up))
    env[a:a + up] = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, up))
    env[a + up:b - down] = 1.0
    if down:
        env[b - down:b] = 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, down))
    return env


left = np.zeros(N)
right = np.zeros(N)

# ---- pad ------------------------------------------------------------------
# Chords overlap by a bar so one dissolves into the next rather than switching.
bars = int(SECONDS / BAR) + 2
for bar in range(bars):
    chord = PROG[bar % len(PROG)]
    env = envelope(bar * BAR, BAR * 2.0, attack=1.5, release=1.9)
    if not env.any():
        continue
    for voice, semi in enumerate(chord):
        f = hz(semi)
        # Two detuned copies, panned apart, for width without a chorus effect.
        for side, (cents, gain) in enumerate(((-4.5, 0.55), (4.5, 0.55))):
            freq = f * 2 ** (cents / 1200)
            # A couple of quiet partials: a pure sine reads as a test tone.
            tone = (np.sin(2 * np.pi * freq * t)
                    + 0.26 * np.sin(2 * np.pi * freq * 2 * t)
                    + 0.09 * np.sin(2 * np.pi * freq * 3 * t))
            # Slow drift, so the pad breathes instead of sitting still.
            tone *= 1 + 0.03 * np.sin(2 * np.pi * (0.07 + voice * 0.013) * t)
            v = tone * env * gain * (0.16 / (1 + voice * 0.55))
            if side == 0:
                left += v * 0.82
                right += v * 0.42
            else:
                left += v * 0.42
                right += v * 0.82

# ---- bells ----------------------------------------------------------------
# One note every two bars, on a chord tone, with a long decay. Sparse on
# purpose: a busy figure turns into a ringtone by the second loop.
rng = np.random.default_rng(7)
beat = 0.0
while beat < SECONDS:
    chord = PROG[int(beat / BAR) % len(PROG)]
    semi = chord[rng.integers(1, len(chord))] + 12
    f = hz(semi)
    a = int(beat * SR)
    span = min(int(3.4 * SR), N - a)
    if span <= 0:
        break
    local = np.arange(span) / SR
    decay = np.exp(-local * 1.45)
    tone = (np.sin(2 * np.pi * f * local)
            + 0.34 * np.sin(2 * np.pi * f * 2.01 * local) * np.exp(-local * 3.2)
            + 0.12 * np.sin(2 * np.pi * f * 3.02 * local) * np.exp(-local * 5.0))
    strike = tone * decay * 0.085
    pan = 0.5 + 0.22 * float(rng.standard_normal())
    pan = min(0.82, max(0.18, pan))
    left[a:a + span] += strike * (1 - pan)
    right[a:a + span] += strike * pan
    beat += BAR * 2

# ---- sub ------------------------------------------------------------------
for bar in range(bars):
    root = hz(PROG[bar % len(PROG)][0] - 12)
    env = envelope(bar * BAR, BAR * 1.6, attack=1.1, release=1.4)
    if not env.any():
        continue
    sub = np.sin(2 * np.pi * root * t) * env * 0.055
    left += sub
    right += sub

# ---- master ---------------------------------------------------------------
# Gentle top and tail so the track arrives and leaves rather than starting and
# stopping, then a soft ceiling.
fade = int(2.6 * SR)
shape = np.ones(N)
shape[:fade] = np.linspace(0, 1, fade) ** 1.6
shape[-fade:] = np.linspace(1, 0, fade) ** 1.4
left *= shape
right *= shape

stereo = np.stack([left, right], axis=1)
stereo = np.tanh(stereo * 1.25) / 1.25              # soft knee, no hard clip
peak = float(np.max(np.abs(stereo))) or 1.0
stereo *= (10 ** (args.peak / 20)) / peak           # leave room for a voiceover

pcm = (stereo * 32767).astype("<i2")
with wave.open(OUT, "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())

print(f"{OUT}  {SECONDS:.1f}s  peak {20 * np.log10(peak * (10 ** (-17/20)) / peak):.0f} dBFS-ish")
