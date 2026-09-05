"""
StarLoader.py  —  Star's standalone plugin loader installer.

Ships with Star and handles placing the bundled core.dll (Pengu Loader's DLL,
MIT-licensed, renamed) next to LeagueClientUx.exe as a version.dll symlink,
plus writing a config file that points plugins_dir at star/plugins/.

Users never see "Pengu". They click "Install Loader" once in Star's UI,
restart League, and plugins work forever.

Directory layout expected next to this file:
    star/
      app.py
      StarLoader.py
      loader/
        core.dll          ← Pengu's compiled DLL (MIT), renamed
      plugins/
        Background-Customizer-V2.js
        ...
"""

import ctypes
import os
import shutil
import sys
from pathlib import Path

import psutil


# ── paths ──────────────────────────────────────────────────────────────────────

def _star_root() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _core_dll_path() -> Path:
    return _star_root() / 'loader' / 'core.dll'


def _plugins_dir() -> Path:
    return _star_root() / 'plugins'


# ── League detection ───────────────────────────────────────────────────────────

def find_league_exe() -> Path | None:
    """Return the path to LeagueClientUx.exe from the running process list."""
    for proc in psutil.process_iter(['name', 'exe']):
        try:
            if proc.info['name'] == 'LeagueClientUx.exe':
                exe = proc.info.get('exe')
                if exe:
                    return Path(exe).resolve()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None


def find_league_dir() -> Path | None:
    """
    Return the folder that contains LeagueClientUx.exe.
    Falls back to common install locations if League isn't running.
    """
    exe = find_league_exe()
    if exe:
        return exe.parent

    # Common fallbacks (Windows).
    # LeagueClientUx.exe lives in the League of Legends root, NOT in the Game/ subfolder.
    candidates = [
        Path(os.environ.get('LOCALAPPDATA', '')) / 'Riot Games' / 'League of Legends',
        Path('C:/Riot Games/League of Legends'),
        Path('C:/Program Files/Riot Games/League of Legends'),
        Path('D:/Riot Games/League of Legends'),
    ]
    for c in candidates:
        if (c / 'LeagueClientUx.exe').exists():
            return c

    return None


# ── install state ──────────────────────────────────────────────────────────────

def _symlink_path(league_dir: Path) -> Path:
    return league_dir / 'version.dll'


def _config_path(league_dir: Path) -> Path:
    """The config file sits next to our DLL, i.e. in the League dir too."""
    return league_dir / 'config'


def _is_our_dll(path: Path, core: Path) -> bool:
    """Check if the file at path is our core.dll (by size+mtime match or symlink target)."""
    if not path.exists():
        return False
    if path.is_symlink():
        try:
            return Path(os.readlink(path)).resolve() == core.resolve()
        except Exception:
            return False
    # Compare file size — fast heuristic; good enough since we wrote it
    try:
        return path.stat().st_size == core.stat().st_size
    except Exception:
        return False


def is_installed() -> dict:
    """
    Return a dict describing the current install state:
      { 'installed': bool, 'league_dir': str|None, 'error': str|None }
    """
    league_dir = find_league_dir()
    if not league_dir:
        return {'installed': False, 'league_dir': None, 'error': 'League client not found. Is it installed?'}

    core = _core_dll_path()
    if not core.exists():
        return {'installed': False, 'league_dir': str(league_dir),
                'error': 'core.dll missing from star/loader/. Reinstall Star.'}

    dll = _symlink_path(league_dir)
    if not dll.exists():
        return {'installed': False, 'league_dir': str(league_dir), 'error': None}

    if _is_our_dll(dll, core):
        return {'installed': True, 'league_dir': str(league_dir), 'error': None}

    return {'installed': False, 'league_dir': str(league_dir),
            'error': 'version.dll already exists in League folder and is not ours. Remove it manually first.'}


# ── install / uninstall ────────────────────────────────────────────────────────

def _is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _elevate_and_rerun(action: str = 'install', league_dir: str | None = None):
    """
    Re-launch THIS script (StarLoader.py) as admin with --star-<action>.
    The elevated process runs the operation and exits — no Flask server.
    """
    args = [f'"{sys.executable}"', f'"{__file__}"', f'--star-{action}']
    if league_dir:
        args.append(f'--league-dir="{league_dir}"')
    ctypes.windll.shell32.ShellExecuteW(
        None, 'runas', sys.executable,
        f'"{__file__}" --star-{action}' + (f' "--league-dir={league_dir}"' if league_dir else ''),
        None, 1
    )


def _write_config(league_dir: Path):
    """
    Write a config file next to our DLL.
    The DLL reads it at startup to know where star/plugins/ is.
    Key: plugins_dir  (exact function name Pengu uses via __func__ trick)
    """
    # Use forward slashes — the C++ config parser reads values literally and does NOT
    # interpret escape sequences, so '\\' would become a literal double-backslash in the path.
    plugins_abs = str(_plugins_dir()).replace('\\', '/')
    config_content = (
        f'# Star Loader config — auto-generated, do not edit manually\n'
        f'plugins_dir = {plugins_abs}\n'
    )
    config_path = league_dir / 'config'
    config_path.write_text(config_content, encoding='utf-8')


def install(league_dir_override: str | None = None) -> dict:
    """
    Install Star's loader into League by copying core.dll as version.dll.
    Copying requires no admin rights — no UAC, no symlink privileges needed.
    Returns { 'ok': bool, 'error': str|None, 'needs_admin': bool }
    """
    league_dir = Path(league_dir_override) if league_dir_override else find_league_dir()
    if not league_dir:
        return {'ok': False, 'error': 'League folder not found. Is League installed and have you provided the correct path?', 'needs_admin': False}

    core = _core_dll_path()
    if not core.exists():
        return {'ok': False, 'error': 'core.dll missing from star/loader/. Reinstall Star.', 'needs_admin': False}

    dll = _symlink_path(league_dir)

    # Already installed (our file is already there)
    if _is_our_dll(dll, core):
        _write_config(league_dir)
        return {'ok': True, 'error': None, 'needs_admin': False}

    # Conflict with a foreign file
    if dll.exists():
        return {'ok': False,
                'error': f'version.dll already exists in {league_dir} and was not placed by Star. '
                          'Remove or rename it manually first.',
                'needs_admin': False}

    try:
        shutil.copy2(str(core), str(dll))
    except PermissionError:
        # copy2 can raise PermissionError when copying metadata/ACLs even if
        # the file bytes were written successfully — check if it actually landed.
        if not _is_our_dll(dll, core):
            return {'ok': False,
                    'error': f'Permission denied writing to {league_dir}. '
                              'Try running Star as Administrator.',
                    'needs_admin': False}
        # file is there and matches — metadata copy failed but data copy succeeded, continue
    except Exception as e:
        return {'ok': False, 'error': str(e), 'needs_admin': False}

    try:
        _write_config(league_dir)
    except Exception:
        pass  # config write failure is non-fatal; DLL is already in place

    return {'ok': True, 'error': None, 'needs_admin': False}


def uninstall(league_dir_override: str | None = None) -> dict:
    """
    Remove Star's loader from League.
    Returns { 'ok': bool, 'error': str|None, 'needs_admin': bool }
    """
    league_dir = Path(league_dir_override) if league_dir_override else find_league_dir()
    if not league_dir:
        return {'ok': False, 'error': 'League folder not found.', 'needs_admin': False}

    dll = _symlink_path(league_dir)
    core = _core_dll_path()

    if not dll.exists():
        return {'ok': True, 'error': None, 'needs_admin': False}

    # Only remove if it's our file
    if not _is_our_dll(dll, core):
        return {'ok': False,
                'error': 'version.dll in the League folder was not placed by Star — not removing.',
                'needs_admin': False}

    try:
        dll.unlink()
        config = _config_path(league_dir)
        if config.exists():
            config.unlink()
        return {'ok': True, 'error': None, 'needs_admin': False}
    except PermissionError:
        return {'ok': False,
                'error': 'Permission denied. Try running Star as Administrator.',
                'needs_admin': False}
    except Exception as e:
        return {'ok': False, 'error': str(e), 'needs_admin': False}


# ── elevated subprocess entry point ───────────────────────────────────────────
# When Star needs admin to create/remove a symlink it re-launches THIS file
# (not app.py) with --star-install or --star-uninstall.  We run the op and
# show a messagebox with the result so the user knows it worked.

if __name__ == '__main__':
    import argparse
    import ctypes as _ctypes

    def _msgbox(title, msg):
        _ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40)  # MB_ICONINFORMATION

    def _errbox(title, msg):
        _ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)  # MB_ICONERROR

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--star-install',   action='store_true')
    parser.add_argument('--star-uninstall', action='store_true')
    parser.add_argument('--league-dir',     default=None)
    args, _ = parser.parse_known_args()

    if args.star_install:
        result = install(args.league_dir)
        if result['ok']:
            _msgbox('Star Loader', 'Loader installed successfully.\nRestart League of Legends to activate.')
        else:
            _errbox('Star Loader — Install Failed', result.get('error') or 'Unknown error')
        sys.exit(0 if result['ok'] else 1)

    elif args.star_uninstall:
        result = uninstall(args.league_dir)
        if result['ok']:
            _msgbox('Star Loader', 'Loader uninstalled successfully.\nRestart League of Legends to deactivate.')
        else:
            _errbox('Star Loader — Uninstall Failed', result.get('error') or 'Unknown error')
        sys.exit(0 if result['ok'] else 1)

    else:
        print('StarLoader.py: no action specified. Use --star-install or --star-uninstall.')
        sys.exit(1)
