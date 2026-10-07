"""
Generate SIMULATED ATC demo clips (not real recordings) for the recorded-clip replay.

Each clip is a scripted transmission in realistic phraseology, spoken with the Windows text-to-speech
voice and processed to sound like narrow-band radio. Fictional callsigns and events. Output:

    atc_clips/<ICAO>/NN.wav   16 kHz mono audio
    atc_clips/<ICAO>/NN.txt   the scripted transcript (used when no speech-to-text is available)

Real recordings can be dropped into the same folders (WAV, 16 kHz mono, plus an optional .txt).

  python demo_atc_clips.py
"""

import wave, subprocess, tempfile
from pathlib import Path
import numpy as np

OUT = Path(__file__).with_name("atc_clips")
RATE = 16000

SCRIPTS = {
    "KDCA": [
        "Reagan Tower, Cactus four seven one two, ready for departure runway one.",
        "Cactus four seven one two, Reagan Tower, wind three four zero at eight, runway one, cleared for takeoff.",
        "Potomac Approach, Speedbird two one seven, level at four thousand.",
        "Speedbird two one seven, Potomac Approach, descend and maintain three thousand, expect the river visual runway one.",
        "All aircraft in the Reagan area, be advised unmanned aircraft reported at four hundred feet, two miles south of the airport near Fort Belvoir. Use caution.",
        "Reagan Tower, Skyhawk eight two Charlie, we just saw a small drone pass off our right wing at about four hundred feet.",
        "Attention all aircraft, a temporary flight restriction has been issued for the national capital region. Check the latest NOTAM before departure.",
        "Delta nine three zero, Reagan Tower, hold short runway one. Ground stop in effect for departures, expect further advisories in ten minutes.",
    ],
    "KDEN": [
        "Denver Tower, United one one four four, runway one six right, ready.",
        "United one one four four, Denver Tower, wind one eight zero at one four gusting two two, runway one six right, cleared for takeoff.",
        "Denver Approach, Southwest eight eight two, level at one one thousand.",
        "Southwest eight eight two, Denver Approach, descend and maintain nine thousand, Denver altimeter two niner niner two.",
        "Denver Tower, Frontier six two four, wind shear alert, loss of fifteen knots on short final runway three five left.",
        "All aircraft in the Denver area, be advised small unmanned aircraft reported two miles north of the field at four hundred feet. Use caution.",
        "Denver Approach, Skyhawk four two Papa, request flight following to Boulder.",
        "Attention all aircraft, ground stop in effect for Denver arrivals due to thunderstorms. Expect further advisories.",
    ],
}


def speak(texts, paths):
    """Render every line with the Windows speech synthesiser (one PowerShell session)."""
    lines = ["Add-Type -AssemblyName System.Speech", "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer", "$s.Rate = 2",
             "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)"]
    for text, path in zip(texts, paths):
        safe = text.replace("'", "''")
        lines += [f"$s.SetOutputToWaveFile('{path}', $f)", f"$s.Speak('{safe}')", "$s.SetOutputToNull()"]
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as f:
        f.write("\n".join(lines))
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", f.name], check=True)


def read_wav(path):
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


def radio(x, rng):
    """Narrow-band, slightly clipped, with squelch tails and static."""
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1 / RATE)
    spec[(freqs < 300) | (freqs > 3000)] = 0
    y = np.fft.irfft(spec, len(x))
    y = np.tanh(3.0 * y / (np.max(np.abs(y)) + 1e-6)) * 0.6
    tail = rng.normal(0, 0.05, int(RATE * 0.25))
    y = np.concatenate([rng.normal(0, 0.05, int(RATE * 0.2)), y, tail])
    y += rng.normal(0, 0.012, len(y))
    return (np.clip(y, -1, 1) * 32767).astype(np.int16)


def main():
    rng = np.random.default_rng(11)
    for icao, lines in SCRIPTS.items():
        folder = OUT / icao
        folder.mkdir(parents=True, exist_ok=True)
        raw = [folder / f"raw_{i + 1:02d}.wav" for i in range(len(lines))]
        speak(lines, [str(p) for p in raw])
        for i, (text, rawp) in enumerate(zip(lines, raw), 1):
            audio = radio(read_wav(rawp), rng)
            with wave.open(str(folder / f"{i:02d}.wav"), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
                w.writeframes(audio.tobytes())
            (folder / f"{i:02d}.txt").write_text(text, encoding="utf-8")
            rawp.unlink()
        print(f"{icao}: {len(lines)} clips")


if __name__ == "__main__":
    main()
