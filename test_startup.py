"""Exercise startup persistence in an isolated, non-startup registry key."""

from pathlib import Path
import sys
import unittest
import uuid

if sys.platform == "win32":
    import winreg
    from startup import StartupRegistration


@unittest.skipUnless(sys.platform == "win32", "Windows registry integration")
class StartupTests(unittest.TestCase):
    def setUp(self):
        self.subkey = rf"Software\RecorderTest_{uuid.uuid4().hex}"
        self.script = Path.cwd() / "folder with spaces" / "recorder.py"
        self.database = Path.cwd() / "data with spaces" / "recordings.sqlite3"
        self.registration = StartupRegistration(
            self.script, self.database, subkey=self.subkey)

    def tearDown(self):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.subkey)
        except FileNotFoundError:
            pass

    def test_enable_persists_and_disable_preserves_unrelated_values(self):
        self.assertFalse(self.registration.is_enabled())
        self.registration.set_enabled(False)
        self.registration.set_enabled(True)
        reopened = StartupRegistration(self.script, self.database, subkey=self.subkey)
        self.assertTrue(reopened.is_enabled())
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.subkey,
                            0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            command, kind = winreg.QueryValueEx(key, self.registration.value_name)
            self.assertEqual(kind, winreg.REG_SZ)
            self.assertIn(f'"{self.script}"', command)
            self.assertIn(f'--db "{self.database}" --start', command)
            winreg.SetValueEx(key, "Unrelated", 0, winreg.REG_SZ, "keep")
        reopened.set_enabled(False)
        self.assertFalse(self.registration.is_enabled())
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.subkey) as key:
            self.assertEqual(winreg.QueryValueEx(key, "Unrelated")[0], "keep")
        reopened.set_enabled(False)

    def test_changed_location_requires_registration_update(self):
        self.registration.set_enabled(True)
        moved = StartupRegistration(self.script.with_name("moved.py"),
                                    self.database, subkey=self.subkey)
        self.assertFalse(moved.is_enabled())
        moved.set_enabled(True)
        self.assertTrue(moved.is_enabled())
        self.assertFalse(self.registration.is_enabled())


if __name__ == "__main__":
    unittest.main()
