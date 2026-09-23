"""Opt-in, current-user Windows startup registration."""

from pathlib import Path
import subprocess
import sys
import winreg


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "MouseDatasetRecorder"


class StartupRegistration:
    def __init__(self, script, database, *, subkey=RUN_KEY, value_name=VALUE_NAME):
        executable = Path(sys.executable)
        windowed = executable.with_name("pythonw.exe")
        if windowed.is_file():
            executable = windowed
        self.command = subprocess.list2cmdline([
            str(executable), str(Path(script).resolve()),
            "--db", str(Path(database).resolve()), "--start",
        ])
        self.subkey = subkey
        self.value_name = value_name

    def is_enabled(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.subkey) as key:
                command, kind = winreg.QueryValueEx(key, self.value_name)
                return kind == winreg.REG_SZ and command == self.command
        except FileNotFoundError:
            return False

    def set_enabled(self, enabled):
        if enabled:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.subkey,
                                    0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, self.value_name, 0, winreg.REG_SZ, self.command)
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.subkey,
                                    0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, self.value_name)
            except FileNotFoundError:
                pass
