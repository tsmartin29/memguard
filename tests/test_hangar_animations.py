"""Hangar screen animations (Nov 4, 2026). Run: python3 -m unittest discover -s tests
Compiles hangar/Hangar.swift into a scratch folder and exercises it without opening anything on screen: HANGAR_FLYOVER=<scene> with
HANGAR_FLYOVER_WAV=<file> writes that scene's synthesised sound to a WAV and exits (no window, no playback). Skipped without swiftc."""
import os, shutil, struct, subprocess, tempfile, unittest, wave
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.environ.get("HANGAR_SWIFT") or os.path.join(HERE, "hangar", "Hangar.swift")
SCENES = {"dive": 3.6, "afterburner": 2.5, "jettison": 2.5, "wheels": 2.7, "blackbox": 3.3, "alpha": 2.4, "cap": 2.2, "scramble": 2.3, "surge": 2.4}

@unittest.skipUnless(shutil.which("swiftc"), "swiftc not installed")
class Sounds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(); cls.exe = os.path.join(cls.d, "hangar_anim")
        subprocess.run(["swiftc", "-o", cls.exe, SRC], check=True, capture_output=True)
    @classmethod
    def tearDownClass(cls): shutil.rmtree(cls.d, ignore_errors=True)
    def wav(self, scene):
        path = os.path.join(self.d, scene + ".wav")
        subprocess.run([self.exe], env={**os.environ, "HANGAR_FLYOVER": scene, "HANGAR_FLYOVER_WAV": path}, check=True, capture_output=True, timeout=60)
        with wave.open(path) as w:
            n = w.getnframes(); data = struct.unpack("<%dh" % n, w.readframes(n)); return w.getframerate(), data
    def test_every_scene_has_sound_of_the_right_length_without_clipping(self):
        for scene, secs in SCENES.items():
            rate, data = self.wav(scene)
            self.assertEqual(rate, 44100); self.assertAlmostEqual(len(data) / rate, secs, delta=0.15, msg=scene)
            peak = max(abs(x) for x in data) / 32767
            self.assertGreater(peak, 0.15, scene)    # audible
            self.assertLess(peak, 0.85, scene)       # soft-clipped, never near full scale
    def test_sound_is_deterministic(self):
        self.assertEqual(self.wav("cap")[1], self.wav("cap")[1])
    def test_one_means_the_dive(self):               # the 30-agent mode plays the A-10 dive; HANGAR_FLYOVER=1 also means the dive
        self.assertEqual(len(self.wav("1")[1]), len(self.wav("dive")[1]))
    def test_ends_quiet(self):
        for scene in ("dive", "wheels", "blackbox"):
            _, data = self.wav(scene); self.assertLess(max(abs(x) for x in data[-200:]), 200, scene)

if __name__ == "__main__": unittest.main()
