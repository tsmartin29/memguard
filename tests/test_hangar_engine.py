"""Hangar engine blip (Nov 5, 2026). Run: python3 -m unittest discover -s tests
Compiles hangar/Hangar.swift into a scratch folder and writes each car's click sound with HANGAR_ENGINE_WAV (no window, nothing plays).
Checks: 2 s or less, audible but not clipped hard, deterministic, quiet at the end, and the pitch follows the rev (idle, peak, idle).
Skipped without swiftc. HANGAR_SWIFT=<file> tests a different copy of the source."""
import os, shutil, struct, subprocess, tempfile, unittest, wave
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.environ.get("HANGAR_SWIFT") or os.path.join(HERE, "hangar", "Hangar.swift")
# car: (idle firing Hz, peak firing Hz) = rpm / 60 * firings per turn (straight six 3, V8 4)
CARS = {"e46": (950 / 60 * 3, 7000 / 60 * 3), "f1": (5000 / 60 * 4, 18000 / 60 * 4)}

def pitch(x, rate, t, lo, hi):
    """Strongest autocorrelation lag between rate/hi and rate/lo in a 2048-sample window at t."""
    a = int(t * rate); seg = x[a:a + 4096]
    best = (-1e30, 1)
    for lag in range(max(2, int(rate / hi)), int(rate / lo) + 1):
        c = sum(seg[i] * seg[i + lag] for i in range(0, 2048, 2))
        if c > best[0]: best = (c, lag)
    return rate / best[1]

@unittest.skipUnless(shutil.which("swiftc"), "swiftc not installed")
class Engine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(); cls.exe = os.path.join(cls.d, "hangar_engine")
        subprocess.run(["swiftc", "-o", cls.exe, SRC], check=True, capture_output=True)
    @classmethod
    def tearDownClass(cls): shutil.rmtree(cls.d, ignore_errors=True)
    def wav(self, car):
        path = os.path.join(self.d, car + ".wav")
        subprocess.run([self.exe], env={**os.environ, "HANGAR_ENGINE": car, "HANGAR_ENGINE_WAV": path}, check=True, capture_output=True, timeout=60)
        with wave.open(path) as w:
            n = w.getnframes(); return w.getframerate(), [v / 32767 for v in struct.unpack("<%dh" % n, w.readframes(n))]
    def test_short_audible_not_clipped(self):
        for car in CARS:
            rate, x = self.wav(car)
            self.assertEqual(rate, 44100); self.assertLessEqual(len(x) / rate, 2.0, car)
            peak = max(abs(v) for v in x); rms = (sum(v * v for v in x) / len(x)) ** 0.5
            self.assertGreater(peak, 0.3, car); self.assertLess(peak, 0.85, car)
            self.assertLess(rms / peak, 0.6, car)   # not squashed into a square wave
            self.assertLess(max(abs(v) for v in x[-200:]), 0.01, car)   # fades out
    def test_deterministic(self):
        self.assertEqual(self.wav("e46")[1], self.wav("e46")[1])
    def test_pitch_follows_the_rev(self):
        for car, (idle, peak) in CARS.items():
            rate, x = self.wav(car)
            p_idle = pitch(x, rate, 0.05, idle * 0.6, idle * 1.6)
            self.assertAlmostEqual(p_idle, idle, delta=idle * 0.2, msg=car)
            p_top = pitch(x, rate, 0.6 if car == "f1" else 0.66, peak * 0.4, peak * 1.3)
            self.assertGreater(p_top, idle * 2.5, car)   # well above idle near the top of the rev

if __name__ == "__main__": unittest.main()
