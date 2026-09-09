"""Windows Service wrapper for JarvisCore/JarvisWeb, replacing the Scheduled-Task-based
setup this machine used before (Get-ScheduledTask "JarvisCore"/"JarvisWeb" -- functionally
close to a service via a boot trigger plus RestartCount=999/RestartInterval=1min, but
invisible to Get-Service/Restart-Service, which is the actual gap this closes).

Deliberately NOT a service-aware rewrite of assistant.main/web_main (handling
SERVICE_CONTROL_STOP inside the app itself) -- this supervises the exact same child
process the Scheduled Task already ran (.venv\\Scripts\\python.exe -m assistant.main /
-m assistant.web_main), the same way NSSM would, just in pure Python via pywin32
(already a project dependency -- nothing new to download or trust).

Install (run once, elevated, using THIS venv's python so pywin32 registers correctly --
_fix_venv_hosting() runs automatically after a successful install, applying two more
fixes found live getting a real service running from a venv for the first time: a local
python3{xy}.dll copy beside the relocated pythonservice.exe, since LocalSystem has no
path to the base install's copy, and a PYTHONPATH set directly in the service's own
Environment registry value, since this Python install is registered only under
HKEY_CURRENT_USER -- invisible to LocalSystem's registry context -- so pythonservice.exe's
own internal `import servicemanager`, done before it ever loads this module, can't
otherwise find pywin32 at all. See _fix_venv_hosting's own docstring for the full story):
    .venv\\Scripts\\python.exe deploy\\windows_service.py --variant core install
    .venv\\Scripts\\python.exe deploy\\windows_service.py --variant web install
    net start JarvisCore
    net start JarvisWeb

Debug (runs in the foreground in this console -- unlike NSSM, pywin32 requires the
service to already be installed for this to work; it does not skip SCM registration):
    .venv\\Scripts\\python.exe deploy\\windows_service.py --variant core debug

Uninstall:
    net stop JarvisCore
    .venv\\Scripts\\python.exe deploy\\windows_service.py --variant core remove
"""
import os
import shutil
import subprocess
import sys
import time
import winreg
from pathlib import Path

# Must run BEFORE the pywin32 imports below, for OUR OWN process's benefit (the actual
# service-hosting fix, for pythonservice.exe's own separate interpreter, is
# _fix_venv_hosting() below -- this sys.path patch alone was NOT sufficient live, see
# its explanation). pythonservice.exe (the compiled host the SCM actually launches)
# embeds its own interpreter using the base Python install's paths -- it does NOT run
# site.py the way an interactive `python.exe` does, so it never processes pywin32's own
# .pth file (which is what normally adds site-packages\win32, \win32\lib, and
# \Pythonwin to sys.path). This is a documented pywin32/venv limitation, not
# misconfiguration (see mhammond/pywin32#1450, "ServiceFramework incompatible with venv").
_SITE_PACKAGES = str(Path(__file__).resolve().parent.parent / ".venv" / "Lib" / "site-packages")
for _sub in ("", "win32", "win32\\lib", "Pythonwin"):
    _p = os.path.join(_SITE_PACKAGES, _sub) if _sub else _SITE_PACKAGES
    if _p not in sys.path:
        sys.path.insert(0, _p)

import servicemanager
import win32event
import win32service
import win32serviceutil

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(REPO_ROOT / ".venv" / "Scripts" / "python.exe")

VARIANTS = {
    "core": {"module": "assistant.main", "name": "JarvisCore", "display": "Jarvis Core"},
    "web": {"module": "assistant.web_main", "name": "JarvisWeb", "display": "Jarvis Web UI"},
}

# How long to wait after a crashed child before restarting it -- matches the Scheduled
# Task's own RestartInterval=1min this replaces, not an arbitrary choice.
RESTART_DELAY_SECONDS = 60
# How long to give the child to exit cleanly on SvcStop before this gives up waiting.
STOP_TIMEOUT_SECONDS = 15


def _make_service_class(variant_key: str):
    variant = VARIANTS[variant_key]

    class _JarvisService(win32serviceutil.ServiceFramework):
        _svc_name_ = variant["name"]
        _svc_display_name_ = variant["display"]
        _svc_description_ = f"Runs {variant['module']} as a supervised background process."

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.process = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)
            if self.process is not None:
                self.process.terminate()

        def SvcDoRun(self):
            # Without this the SCM waits for a RUNNING status that never arrives and
            # gives up with "the service is not responding to the control function" --
            # found live: the smoketest service installed fine but could not start
            # until this call was added.
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""))
            self._run_loop()

        def _stopped(self) -> bool:
            return win32event.WaitForSingleObject(self.stop_event, 0) == win32event.WAIT_OBJECT_0

        def _run_loop(self):
            # Restart on crash, same policy as the Scheduled Task's RestartCount=999/
            # RestartInterval=1min -- a supervised child dying is not a reason to take
            # the whole service down, only a real SvcStop is.
            while not self._stopped():
                self.process = subprocess.Popen([PYTHON, "-m", variant["module"]], cwd=str(REPO_ROOT))
                while self.process.poll() is None:
                    if win32event.WaitForSingleObject(self.stop_event, 1000) == win32event.WAIT_OBJECT_0:
                        self.process.terminate()
                        try:
                            self.process.wait(timeout=STOP_TIMEOUT_SECONDS)
                        except subprocess.TimeoutExpired:
                            self.process.kill()
                        return
                if self._stopped():
                    return
                servicemanager.LogMsg(
                    servicemanager.EVENTLOG_WARNING_TYPE, servicemanager.PYS_SERVICE_STARTED,
                    (self._svc_name_,
                     f"child process exited with code {self.process.returncode}, "
                     f"restarting in {RESTART_DELAY_SECONDS}s"))
                if win32event.WaitForSingleObject(self.stop_event, RESTART_DELAY_SECONDS * 1000) == win32event.WAIT_OBJECT_0:
                    return

    _JarvisService.__name__ = f"Jarvis{variant_key.capitalize()}Service"
    return _JarvisService


# Built at MODULE level -- outside the `if __name__` guard -- and bound into globals()
# unconditionally, not just when this file is run as __main__. Confirmed live this
# matters: install writes "PythonClass = windows_service.JarvisCoreService" into the
# registry, and pythonservice.exe later does a real `import windows_service` (a plain
# module import, never executing the __main__ block) followed by
# `getattr(module, "JarvisCoreService")` to actually run the service -- which failed
# with AttributeError until the classes existed as real module attributes regardless
# of how the file was loaded.
SERVICE_CLASSES = {key: _make_service_class(key) for key in VARIANTS}
globals().update({cls.__name__: cls for cls in SERVICE_CLASSES.values()})


def _fix_venv_hosting(service_name: str) -> None:
    """Two more fixes `install` alone doesn't cover, both found live getting a real
    service running from this venv for the first time -- run automatically after every
    install so this can't be forgotten on a reinstall:

    1. pythonservice.exe was moved into .venv\\ (by pywin32's own install step) but has
       no python3{xy}.dll beside it and isn't on LocalSystem's DLL search path to the
       base install where one lives -- confirmed live: without a local copy, the
       service host can't even load the interpreter. Venvs don't ship their own copy of
       this DLL by design (sys.base_prefix is where the real one lives), so it has to be
       copied in explicitly.
    2. Even with the DLL fixed, pythonservice.exe's own internal `import servicemanager`
       (done before it ever loads OUR module) still failed with ModuleNotFoundError --
       traced to this Python install being registered only under HKEY_CURRENT_USER
       (a per-user install), which LocalSystem's registry context can't see at all.
       Rather than also duplicating that registration under HKLM (a machine-wide change
       with a bigger blast radius than this one service needs), this sets PYTHONPATH
       directly in the service's own Environment registry value, which the SCM applies
       to the process it launches -- scoped to just this service.
    """
    dll_name = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    venv_dir = REPO_ROOT / ".venv"
    for name in (dll_name, "python3.dll"):
        dst = venv_dir / name
        if not dst.exists():
            src = Path(sys.base_prefix) / name
            if src.exists():
                shutil.copy2(src, dst)
                print(f"copied {src} -> {dst}")

    site_packages = venv_dir / "Lib" / "site-packages"
    python_path = ";".join(str(site_packages / sub) if sub else str(site_packages)
                            for sub in ("", "win32", "win32\\lib", "Pythonwin"))
    key_path = rf"SYSTEM\CurrentControlSet\Services\{service_name}"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Environment", 0, winreg.REG_MULTI_SZ, [f"PYTHONPATH={python_path}"])
    print(f"set PYTHONPATH in {service_name}'s service Environment")


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "--variant" or sys.argv[2] not in VARIANTS:
        print(__doc__)
        sys.exit(1)
    variant_key = sys.argv[2]
    service_class = SERVICE_CLASSES[variant_key]
    # Strip our own "--variant <key>" so what's left looks like a normal
    # `python script.py <command>` invocation, which is what pywin32's own argument
    # parsing (install/remove/start/stop/debug) expects.
    sys.argv = [sys.argv[0]] + sys.argv[3:]
    win32serviceutil.HandleCommandLine(service_class)
    if len(sys.argv) > 1 and sys.argv[1] == "install":
        _fix_venv_hosting(VARIANTS[variant_key]["name"])
