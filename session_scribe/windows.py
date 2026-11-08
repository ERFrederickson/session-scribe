"""Make Session Scribe behave like a normal Windows app.

- A Start Menu shortcut with the app icon (right-click it, Pin to taskbar)
- A fixed AppUserModelID shared by the shortcut and the running process, so
  the window groups under the pinned icon instead of showing up as "Python"
- The window's own icon (title bar, Alt+Tab)
- Installing updated packages when requirements.txt changed after a git pull
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

APP_ID = "ERFrederickson.SessionScribe"
APP_NAME = "Session Scribe"
REPO_ROOT = Path(__file__).resolve().parents[1]
ICON = Path(__file__).resolve().parent / "assets" / "icon.ico"

IS_WINDOWS = sys.platform == "win32"


def start_menu_shortcut() -> Path:
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / f"{APP_NAME}.lnk")


def venv_python(windowed: bool) -> Path:
    exe = Path(sys.executable)
    return exe.with_name("pythonw.exe" if windowed else "python.exe")


# ----------------------------------------------------------- app identity --

def set_app_id() -> None:
    if not IS_WINDOWS:
        return
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        log.exception("Could not set AppUserModelID")


def ensure_shortcut(force: bool = False) -> Path | None:
    """Create or refresh the Start Menu shortcut. Safe to call on every launch."""
    if not IS_WINDOWS:
        return None
    path = start_menu_shortcut()
    target = venv_python(windowed=True)
    try:
        import pythoncom
        from win32com.propsys import propsys, pscon
        from win32com.shell import shell

        if path.exists() and not force:
            # Refresh only if it points somewhere else (e.g. the repo moved).
            link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None,
                                              pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
            link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(path))
            current, _ = link.GetPath(0)
            if Path(current) == target:
                return path

        link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None,
                                          pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
        link.SetPath(str(target))
        link.SetArguments("-m session_scribe")
        link.SetWorkingDirectory(str(REPO_ROOT))
        link.SetIconLocation(str(ICON), 0)
        link.SetDescription("Record and transcribe Discord sessions")
        store = link.QueryInterface(propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID, pythoncom.VT_LPWSTR))
        store.Commit()
        path.parent.mkdir(parents=True, exist_ok=True)
        link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(path), 0)
        log.info("Created Start Menu shortcut at %s", path)
        return path
    except Exception:
        log.exception("Could not create the Start Menu shortcut")
        return None


def set_window_icon(title: str) -> None:
    """Give the window our icon (pywebview on Windows otherwise shows Python's)."""
    if not IS_WINDOWS or not ICON.exists():
        return
    import ctypes

    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return
    IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x80
    for which, size in ((0, 16), (1, 32)):  # ICON_SMALL, ICON_BIG
        hicon = user32.LoadImageW(None, str(ICON), IMAGE_ICON, size, size, LR_LOADFROMFILE)
        if hicon:
            user32.SendMessageW(hwnd, WM_SETICON, which, hicon)


# --------------------------------------------------------------- packages --

def ensure_packages() -> None:
    """If requirements.txt changed (after a git pull), install before starting.

    Launching from the Start Menu skips Session Scribe.bat, which normally does
    this, so the app checks for itself.
    """
    req = REPO_ROOT / "requirements.txt"
    marker = Path(sys.prefix) / "installed-requirements.txt"
    try:
        if marker.exists() and marker.read_bytes() == req.read_bytes():
            return
    except OSError:
        return
    if not IS_WINDOWS:
        return
    log.info("requirements.txt changed, installing packages")
    python = venv_python(windowed=False)
    CREATE_NEW_CONSOLE = 0x10
    script = (
        f'"{python}" -m pip install -r "{req}" && copy /y "{req}" "{marker}" >nul '
        f'|| (echo. & echo Installing packages failed. & pause)'
    )
    # /s makes cmd strip exactly the outer quotes, so the quoted paths inside
    # survive. A string (not a list) is passed to CreateProcess unchanged.
    subprocess.run(f'cmd /s /c "title Updating Session Scribe & {script}"',
                   creationflags=CREATE_NEW_CONSOLE, cwd=str(REPO_ROOT))
