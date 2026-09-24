"""Opt-in, current-user startup registration (Windows Run key or macOS LaunchAgent)."""

from pathlib import Path
import plistlib
import subprocess
import sys

if sys.platform == "win32":
    import winreg


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "MouseDatasetRecorder"


class WindowsStartupRegistration:
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


LAUNCH_AGENT_LABEL = "com.mousedatasetrecorder.recorder"


class StartupPermissionError(PermissionError):
    """Startup folder not writable; `fix_command` is a Terminal command that fixes it."""

    def __init__(self, message, fix_command):
        super().__init__(message)
        self.fix_command = fix_command


class MacStartupRegistration:
    def __init__(self, script, database, *, directory=None, label=LAUNCH_AGENT_LABEL):
        directory = Path(directory or Path.home() / "Library" / "LaunchAgents")
        self.path = directory / f"{label}.plist"
        self.label = label
        self.arguments = [
            sys.executable, str(Path(script).resolve()),
            "--db", str(Path(database).resolve()), "--start",
        ]

    def is_enabled(self):
        try:
            with self.path.open("rb") as handle:
                job = plistlib.load(handle)
        except FileNotFoundError:
            return False
        except plistlib.InvalidFileException:
            return False
        return job.get("ProgramArguments") == self.arguments

    def set_enabled(self, enabled):
        if enabled:
            job = {
                "Label": self.label,
                "ProgramArguments": self.arguments,
                "RunAtLoad": True,
                # Only start in a logged-in GUI session, never before login.
                "LimitLoadToSessionType": "Aqua",
            }
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("wb") as handle:
                    plistlib.dump(job, handle)
            except PermissionError as error:
                raise self._permission_error(error) from error
        else:
            try:
                self.path.unlink(missing_ok=True)
            except PermissionError as error:
                raise self._permission_error(error) from error

    def _permission_error(self, error):
        # Some installers leave ~/Library/LaunchAgents owned by root.
        folder = self.path.parent
        shown = ("~/Library/LaunchAgents" if folder == Path.home() / "Library" / "LaunchAgents"
                 else f'"{folder}"')
        command = f'sudo chown "$USER" {shown}'
        return StartupPermissionError(
            f"macOS does not let this account write to {shown}, so the recorder "
            f"cannot register itself to start at sign-in. This happens when another "
            f"installer left that folder owned by the system.\n\n"
            f"To fix it, run this in Terminal, enter your Mac password, then turn "
            f"the setting on again:\n\n{command}", command)


StartupRegistration = (MacStartupRegistration if sys.platform == "darwin"
                       else WindowsStartupRegistration)
