import sys
import os
import subprocess
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, jsonify, request, send_from_directory

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from AutoAccept import AutoAccept
from Backgrounds import change_profile_background, fetch_all_champion_skins
from Badges import change_profile_badges
from Config import get_automation_delay, load_config, save_config
from disconnect_reconnect_chat import Chat
from Dodge import dodge
from Icons import change_profile_icon, fetch_all_profile_icons
from Iconsclient import icon_client
from InstalockAutoban import InstalockAutoban
from RemoveFriends import get_friends, remove_all_friends
from Rengar import Rengar, find_league_client_credentials
from RestartUX import restart
from Reveal import REVEAL_PROVIDERS, reveal
from Riotidchanger import change_riotid
from LobbyStats import get_lobby_stats
from DraftHelper import get_draft_suggestions
from StatusChanger import change_status
import StarLoader
from PluginLoader import PluginLoader

app = Flask(__name__, static_folder='static', template_folder='templates')

# Plugins directory: star/plugins/ next to the app
def _plugins_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent / 'plugins'
    return Path(__file__).resolve().parent / 'plugins'

PLUGINS_DIR = _plugins_dir()
PLUGINS_DIR.mkdir(parents=True, exist_ok=True)

def _pl_log(lvl, msg):
    add_activity(lvl, msg)

plugin_loader = PluginLoader(PLUGINS_DIR, log_fn=_pl_log)

config = load_config()
connected = False
account_name = ""
account_tag = ""
account_region = ""
account_text = ""
chat_obj = None
rengar_api = None
activity_log = []
_stopping = False
_monitors_started = False
_last_events = {}
_cached_skins = []
_cached_icons = []

# ── Loader state ──────────────────────────────────────────────────────────────
# Install is manual only — no auto-install watchdog.
_loader_watchdog_done = False
_loader_ux_restarted  = False

auto_accept_obj = None
champion_automation = None


def init_automations():
    global auto_accept_obj, champion_automation
    def cb(level, message):
        add_activity(level, message)
    auto_accept_obj = AutoAccept(config, cb)
    champion_automation = InstalockAutoban(config, cb)

init_automations()


def add_activity(level, message):
    now = time.monotonic()
    key = (level, str(message))
    if level == 'error' and now - _last_events.get(key, 0) < 10:
        return
    _last_events[key] = now
    activity_log.append({
        'time': datetime.now().strftime('%H:%M'),
        'level': level,
        'message': str(message)
    })
    if len(activity_log) > 200:
        activity_log.pop(0)


def _fix_loader_config(league_dir):
    """
    Rewrite the Pengu config next to version.dll with the correct absolute
    plugins_dir path. StarLoader computes this at install time from its own
    location, which can be wrong if the exe moved. We always rewrite it with
    PLUGINS_DIR which is resolved at runtime from app.py's location.
    """
    try:
        if not league_dir:
            return
        cfg_path = Path(league_dir) / 'config'
        cfg_path.mkdir(parents=True, exist_ok=True)
        plugins_path = str(PLUGINS_DIR).replace('\\', '/')
        config_file = cfg_path / 'core'
        config_file.write_text(
            f'# Star Loader config — auto-generated, do not edit manually\nplugins_dir = {plugins_path}\n',
            encoding='utf-8'
        )
        add_activity('system', f'Plugin config written: {plugins_path}')
    except Exception as e:
        add_activity('warning', f'Could not rewrite loader config: {e}')


def connection_loop():
    global connected, account_name, account_tag, account_region, account_text, chat_obj, rengar_api, _monitors_started
    global _loader_watchdog_done, _loader_ux_restarted
    prev = (None, None)
    while not _stopping:
        port, token = find_league_client_credentials()
        creds = (port, token) if port and token else (None, None)
        if creds != (None, None) and creds != prev:
            try:
                api = Rengar()
                sr = api.lcu_request('GET', '/lol-summoner/v1/current-summoner', '')
                rr = api.lcu_request('GET', '/riotclient/region-locale', '')
                if sr.status_code == 200:
                    s = sr.json()
                    account_name = s.get('gameName', '')
                    account_tag = s.get('tagLine', '')
                    account_region = rr.json().get('webRegion', '').upper() if rr.status_code == 200 else ''
                    account_text = f"{account_name}#{account_tag}" + (f" ({account_region})" if account_region else "")
                else:
                    account_name = "League Client"
                    account_tag = ""
                    account_text = "Connected"

                try:
                    chat_obj = Chat(api)
                except Exception:
                    chat_obj = None

                rengar_api = api
                auto_accept_obj.rengar = api
                champion_automation.rengar = api
                connected = True
                add_activity('system', 'Connected to League Client')

                # Pre-load champion dict
                try:
                    champion_automation.update_champion_list()
                except Exception:
                    pass

                if not _monitors_started:
                    _monitors_started = True
                    t1 = threading.Thread(target=auto_accept_obj.monitor_queue, daemon=True)
                    t2 = threading.Thread(target=champion_automation.monitor_champ_select, daemon=True)
                    t1.start()
                    t2.start()

            except Exception as e:
                add_activity('error', f'Client connection: {e}')
                creds = (None, None)

        elif prev != (None, None) and creds == (None, None):
            connected = False
            chat_obj = None
            rengar_api = None
            account_name = ""
            account_tag = ""
            account_region = ""
            account_text = ""
            add_activity('warning', 'League Client disconnected')

        prev = creds
        time.sleep(2)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(app.static_folder, 'favicon.ico')


@app.route('/api/status')
def api_status():
    provider = config.get('lobby_reveal', {}).get('provider', 'porofessor')
    chat_state = chat_obj.return_state() if chat_obj else "--"

    rc_cfg = config.get('riot_client', {})
    return jsonify({
        'connected': connected,
        'account_name': account_name,
        'account_tag': account_tag,
        'account_region': account_region,
        'account_text': account_text,
        'features': {
            'auto_accept': bool(auto_accept_obj.auto_accept_enabled),
            'instalock_enabled': bool(champion_automation.instalock_enabled),
            'instalock_champion': champion_automation.instalock_champion or 'Random',
            'autoban_enabled': bool(champion_automation.auto_ban_enabled),
            'autoban_champion': champion_automation.auto_ban_champion or 'None',
            'chat_state': chat_state,
            'reveal_provider': REVEAL_PROVIDERS.get(provider, 'Porofessor'),
        },
        'config': {
            'auto_accept_delay': get_automation_delay(config, 'auto_accept', 0.0),
            'instalock_delay': get_automation_delay(config, 'instalock', 0.3),
            'autoban_delay': get_automation_delay(config, 'autoban', 0.3),
            'lobby_reveal_provider': provider,
            'riot_client_auto_launch': bool(rc_cfg.get('auto_launch', False)),
            'riot_client_path': rc_cfg.get('path', ''),
        }
    })


@app.route('/api/activity')
def api_activity():
    since = int(request.args.get('since', 0))
    return jsonify({'entries': activity_log[since:], 'total': len(activity_log)})


# ── Feature endpoints ─────────────────────────────────────────────────────────

@app.route('/api/auto-accept/toggle', methods=['POST'])
def toggle_auto_accept():
    if not connected:
        return jsonify({'ok': False, 'error': 'League Client not connected'})
    auto_accept_obj.toggle_auto_accept()
    return jsonify({'ok': True, 'enabled': bool(auto_accept_obj.auto_accept_enabled)})


@app.route('/api/instalock/toggle', methods=['POST'])
def toggle_instalock():
    if not connected:
        return jsonify({'ok': False, 'error': 'League Client not connected'})
    state = champion_automation.toggle_instalock()
    return jsonify({
        'ok': True,
        'enabled': bool(state),
        'champion': champion_automation.instalock_champion
    })


@app.route('/api/autoban/toggle', methods=['POST'])
def toggle_autoban():
    if not connected:
        return jsonify({'ok': False, 'error': 'League Client not connected'})
    state = champion_automation.toggle_auto_ban()
    return jsonify({
        'ok': True,
        'enabled': bool(state),
        'champion': champion_automation.auto_ban_champion
    })


@app.route('/api/champions')
def get_champions():
    if not connected:
        return jsonify({'ok': False, 'error': 'Not connected', 'champions': []})
    try:
        if rengar_api:
            champion_automation.rengar = rengar_api
        champs = champion_automation.update_champion_list()
        return jsonify({'ok': True, 'champions': ['Random'] + [c.title() for c in champs]})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e), 'champions': []})


@app.route('/api/champion/set', methods=['POST'])
def set_champion():
    if not connected:
        return jsonify({'ok': False, 'error': 'Not connected'})
    data = request.json or {}
    mode = data.get('mode')  # 'instalock' or 'autoban'
    champion = data.get('champion')
    if not champion:
        return jsonify({'ok': False, 'error': 'No champion specified'})
    try:
        if rengar_api:
            champion_automation.rengar = rengar_api
        champion_automation.update_champion_list()
        if mode == 'instalock':
            champion_automation.set_instalock_champion(champion)
            add_activity('success', f'Instalock set to {champion}')
        else:
            champion_automation.set_auto_ban_champion(champion)
            add_activity('success', f'AutoBan set to {champion}')
        return jsonify({'ok': True, 'champion': champion})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/chat/toggle', methods=['POST'])
def toggle_chat():
    if not connected:
        return jsonify({'ok': False, 'error': 'Not connected'})
    global chat_obj
    try:
        if not chat_obj and rengar_api:
            chat_obj = Chat(rengar_api)
        if chat_obj:
            chat_obj.toggle_chat()
            state = chat_obj.return_state()
            add_activity('info', f'Chat is now {state}')
            return jsonify({'ok': True, 'state': state})
        return jsonify({'ok': False, 'error': 'Chat unavailable'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/icon', methods=['POST'])
def set_icon():
    if not connected:
        return jsonify({'ok': False, 'error': 'Not connected'})
    data = request.json or {}
    icon_id = data.get('icon_id')
    client_only = data.get('client_only', False)
    try:
        if client_only:
            result = icon_client(icon_id)
        else:
            result = change_profile_icon(icon_id)
        add_activity('success', f"{'Client' if client_only else 'Profile'} icon changed to {result}")
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/skins')
def get_skins():
    global _cached_skins
    if _cached_skins:
        return jsonify({'ok': True, 'skins': _cached_skins})
    try:
        _cached_skins = fetch_all_champion_skins()
        return jsonify({'ok': True, 'skins': _cached_skins})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e), 'skins': []})


@app.route('/api/icons')
def get_icons():
    global _cached_icons
    if _cached_icons:
        return jsonify({'ok': True, 'icons': _cached_icons})
    try:
        _cached_icons = fetch_all_profile_icons()
        return jsonify({'ok': True, 'icons': _cached_icons})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e), 'icons': []})


@app.route('/api/background', methods=['POST'])
def set_background():
    if not connected:
        return jsonify({'ok': False, 'error': 'Not connected'})
    data = request.json or {}
    skin_id = data.get('skin_id')
    skin_name = data.get('skin_name', str(skin_id))
    try:
        change_profile_background(skin_id, rengar=rengar_api)
        add_activity('success', f'Background changed to {skin_name}')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/riotid', methods=['POST'])
def set_riotid():
    if not connected:
        return jsonify({'ok': False, 'error': 'Not connected'})
    data = request.json or {}
    name = data.get('name', '').strip()
    tag = data.get('tag', '').strip().lstrip('#')
    if not name or not tag:
        return jsonify({'ok': False, 'error': 'Name and tag are required'})
    if len(name) > 16:
        return jsonify({'ok': False, 'error': 'Name max 16 chars'})
    if len(tag) > 5:
        return jsonify({'ok': False, 'error': 'Tag max 5 chars'})
    try:
        result = change_riotid(name, tag)
        add_activity('success', f'Riot ID changed to {result}')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/badges', methods=['POST'])
def set_badges():
    if not connected:
        return jsonify({'ok': False, 'error': 'Not connected'})
    data = request.json or {}
    mode = data.get('mode', 'empty')
    glitched_id = data.get('glitched_id', '0')
    try:
        change_profile_badges(mode, glitched_id)
        add_activity('success', 'Profile badges updated')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/status-msg', methods=['POST'])
def set_status_msg():
    if not connected:
        return jsonify({'ok': False, 'error': 'Not connected'})
    data = request.json or {}
    try:
        change_status(data.get('status', ''))
        add_activity('success', 'Status message updated')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/reveal', methods=['POST'])
def run_reveal():
    if not connected:
        return jsonify({'ok': False, 'error': 'League Client not connected'})
    provider = config.get('lobby_reveal', {}).get('provider', 'porofessor')
    if provider not in REVEAL_PROVIDERS:
        provider = 'porofessor'
    try:
        url = reveal(provider=provider, rengar=rengar_api, open_browser=True)
        add_activity('success', f'Lobby revealed on {REVEAL_PROVIDERS.get(provider, "Porofessor")}')
        return jsonify({'ok': True, 'url': url})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/lobby-stats', methods=['GET'])
def get_lobby_stats_api():
    if not connected:
        return jsonify({'status': 'error', 'message': 'League Client not connected'}), 400
    try:
        data = get_lobby_stats(rengar=rengar_api)
        return jsonify({'status': 'ok', 'data': data})
    except RuntimeError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Unexpected error: {e}'}), 500


@app.route('/api/auto-reveal', methods=['POST'])
def set_auto_reveal():
    data = request.get_json() or {}
    enabled = bool(data.get('enabled', False))
    config.setdefault('lobby_reveal', {})['auto_reveal'] = enabled
    save_config(config)
    return jsonify({'status': 'ok', 'auto_reveal': enabled})


@app.route('/api/draft-suggestions', methods=['GET'])
def draft_suggestions():
    if not connected:
        return jsonify({'status': 'error', 'message': 'League Client not connected'}), 400
    rank = request.args.get('rank', 'emerald')
    if rank not in ('emerald', 'master'):
        rank = 'emerald'
    try:
        data = get_draft_suggestions(rank_key=rank, rengar=rengar_api)
        return jsonify({'status': 'ok', 'data': data})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/dodge', methods=['POST'])
def run_dodge():
    if not connected:
        return jsonify({'ok': False, 'error': 'League Client not connected'})
    try:
        dodge(rengar=rengar_api)
        add_activity('success', 'Champion select dodged')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/restart-ux', methods=['POST'])
def run_restart_ux():
    if not connected:
        return jsonify({'ok': False, 'error': 'League Client not connected'})
    try:
        restart()
        add_activity('success', 'Client UX restart requested')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/remove-friends', methods=['POST'])
def run_remove_friends():
    if not connected:
        return jsonify({'ok': False, 'error': 'League Client not connected'})
    try:
        friends = get_friends()
        count = remove_all_friends(friends)
        add_activity('success', f'Removed {count} friends')
        return jsonify({'ok': True, 'count': count})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/settings', methods=['POST'])
def save_settings_route():
    data = request.json or {}
    config.setdefault('lobby_reveal', {})['provider'] = data.get('provider', 'porofessor')
    config.setdefault('auto_accept', {})['delay_seconds'] = float(data.get('auto_accept_delay', 0.0))
    config.setdefault('instalock', {})['delay_seconds'] = float(data.get('instalock_delay', 0.3))
    config.setdefault('autoban', {})['delay_seconds'] = float(data.get('autoban_delay', 0.3))
    # Riot Client auto-launch
    if 'riot_client_auto_launch' in data:
        config.setdefault('riot_client', {})['auto_launch'] = bool(data['riot_client_auto_launch'])
    if 'riot_client_path' in data:
        config.setdefault('riot_client', {})['path'] = str(data['riot_client_path'])
    # Loader auto-install toggle
    if 'loader_enabled' in data:
        config.setdefault('loader', {})['enabled'] = bool(data['loader_enabled'])
    save_config(config)
    add_activity('success', 'Configuration saved')
    return jsonify({'ok': True})


# ── Loader / Plugin API ───────────────────────────────────────────────────────

def _get_plugins():
    """Return list of plugin dicts from the plugins/ directory (Pengu-style)."""
    plugins = []
    if not PLUGINS_DIR.exists():
        return plugins

    def parse_meta(content):
        import re
        author = ''
        link = ''
        desc = ''
        m = re.search(r'@author\s+(.+)', content)
        if m:
            a = m.group(1).strip()
            author = a if '#' in a else '@' + a
        m = re.search(r'@link\s+(https?://\S+)', content)
        if m:
            link = m.group(1).strip()
        m = re.search(r'@description\s+(.+)', content)
        if m:
            desc = m.group(1).strip()
        return author, link, desc

    def add_plugin(name, path, enabled, author_hint=''):
        try:
            content = Path(path).read_text(encoding='utf-8', errors='replace')
        except Exception:
            content = ''
        author, link, desc = parse_meta(content)
        plugins.append({
            'name': name,
            'path': str(path),
            'enabled': enabled,
            'author': author or author_hint,
            'link': link,
            'description': desc,
        })

    # Sub-folder plugins: plugins/myplugin/index.js or plugins/@scope/myplugin/index.js
    for item in sorted(PLUGINS_DIR.iterdir()):
        if item.name.startswith('.') or item.name.startswith('_'):
            continue
        if item.is_dir():
            if item.name.startswith('@'):
                for sub in sorted(item.iterdir()):
                    if sub.name.startswith('.') or sub.name.startswith('_') or not sub.is_dir():
                        continue
                    idx = sub / 'index.js'
                    idx_dis = sub / 'index.js_'
                    if idx.exists():
                        add_plugin(f'{item.name}/{sub.name}', idx, True, item.name)
                    elif idx_dis.exists():
                        add_plugin(f'{item.name}/{sub.name}', idx_dis, False, item.name)
            else:
                idx = item / 'index.js'
                idx_dis = item / 'index.js_'
                if idx.exists():
                    add_plugin(item.name, idx, True)
                elif idx_dis.exists():
                    add_plugin(item.name, idx_dis, False)

    # Top-level .js / .js_ files
    for item in sorted(PLUGINS_DIR.iterdir()):
        if item.name.startswith('.') or item.name.startswith('_') or item.is_dir():
            continue
        if item.suffix == '.js':
            add_plugin(item.name, item, True)
        elif item.name.endswith('.js_'):
            add_plugin(item.name.rstrip('_'), item, False)

    return plugins


@app.route('/api/plugins')
def api_plugins():
    try:
        plugins = _get_plugins()
        riot_cfg = config.get('riot_client', {})
        return jsonify({
            'ok': True,
            'plugins': plugins,
            'riot_client': {
                'auto_launch': bool(riot_cfg.get('auto_launch', False)),
                'path': riot_cfg.get('path', 'C:\\Riot Games\\Riot Client\\RiotClientElectron\\Riot Client.exe'),
            },
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/plugins/toggle', methods=['POST'])
def api_plugin_toggle():
    data = request.json or {}
    path_str = data.get('path', '')
    try:
        p = Path(path_str)
        if not p.exists():
            return jsonify({'ok': False, 'error': 'Plugin file not found'})
        # Security: must be inside PLUGINS_DIR
        p.resolve().relative_to(PLUGINS_DIR.resolve())
        if p.name.endswith('.js_'):
            # disabled → enable: strip trailing underscore
            new_p = p.with_name(p.name[:-1])
            p.rename(new_p)
            enabled = True
        elif p.name.endswith('.js'):
            # enabled → disable: append underscore
            new_p = Path(str(p) + '_')
            p.rename(new_p)
            enabled = False
        else:
            return jsonify({'ok': False, 'error': 'Unknown plugin extension'})
        add_activity('info', f'Plugin {"enabled" if enabled else "disabled"}: {p.name}')
        # Reload CDP injection so change takes effect immediately
        plugin_loader.reload()
        # Ask League client to restart UX renderer if connected
        try:
            if connected and rengar_api:
                restart()
        except Exception:
            pass
        return jsonify({'ok': True, 'enabled': enabled, 'new_path': str(new_p)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/plugins/open-folder', methods=['POST'])
def api_open_plugins_folder():
    try:
        import subprocess as sp
        folder = str(PLUGINS_DIR.resolve())
        if sys.platform == 'win32':
            sp.Popen(['explorer', folder])
        elif sys.platform == 'darwin':
            sp.Popen(['open', folder])
        else:
            sp.Popen(['xdg-open', folder])
        return jsonify({'ok': True, 'path': folder})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


def _is_riot_client_running():
    """Check if RiotClientElectron or Riot Client process is running."""
    import psutil
    target_names = {'riotclientelectron', 'riot client', 'riotclientservices', 'riotclientux'}
    try:
        for proc in psutil.process_iter(['name']):
            if proc.info['name'] and proc.info['name'].lower().replace('.exe', '') in target_names:
                return True
    except Exception:
        pass
    return False


def _launch_riot_client():
    """Launch the Riot Client exe from config if auto_launch is on and client not running."""
    rc_cfg = config.get('riot_client', {})
    if not rc_cfg.get('auto_launch', False):
        return
    exe_path = rc_cfg.get('path', '').strip()
    if not exe_path or not os.path.isfile(exe_path):
        add_activity('warning', f'Riot Client path not found: {exe_path}')
        return
    if _is_riot_client_running():
        return
    try:
        subprocess.Popen([exe_path], close_fds=True)
        add_activity('system', f'Riot Client launched: {os.path.basename(exe_path)}')
    except Exception as e:
        add_activity('error', f'Failed to launch Riot Client: {e}')


@app.route('/api/loader/status')
def api_loader_status():
    try:
        status = StarLoader.is_installed()
        status['auto_enabled'] = bool(config.get('loader', {}).get('enabled', False))
        return jsonify({'ok': True, **status})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/loader/install', methods=['POST'])
def api_loader_install():
    global _loader_watchdog_done, _loader_ux_restarted
    data = request.json or {}
    league_dir = data.get('league_dir') or None
    result = StarLoader.install(league_dir)
    if result['ok']:
        # Always rewrite the plugin config with the correct runtime path
        resolved_league_dir = league_dir or result.get('league_dir')
        _fix_loader_config(resolved_league_dir)
        if connected:
            add_activity('system', 'Loader installed — restarting League UX to activate')
            def _kick_ux():
                time.sleep(1)
                try:
                    restart(rengar=rengar_api)
                    add_activity('system', 'League UX restarted — plugins now active')
                except Exception as e:
                    add_activity('warning', f'UX restart failed: {e}')
            threading.Thread(target=_kick_ux, daemon=True).start()
        else:
            add_activity('system', 'Loader installed — start League to activate plugins')
    else:
        add_activity('error', f"Loader install failed: {result.get('error')}")
    return jsonify(result)


@app.route('/api/loader/uninstall', methods=['POST'])
def api_loader_uninstall():
    global _loader_watchdog_done, _loader_ux_restarted
    data = request.json or {}
    league_dir = data.get('league_dir') or None
    result = StarLoader.uninstall(league_dir)
    if result['ok']:
        _loader_watchdog_done = False
        _loader_ux_restarted = False
        add_activity('system', 'Loader uninstalled — restart League to deactivate')
    else:
        add_activity('error', f"Loader uninstall failed: {result.get('error')}")
    return jsonify(result)


if __name__ == '__main__':
    add_activity('system', 'Star started')
    _launch_riot_client()
    plugin_loader.start()
    threading.Thread(target=connection_loop, daemon=True).start()
    def open_browser():
        time.sleep(1.0)
        webbrowser.open('http://localhost:5199')
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host='0.0.0.0', port=5199, debug=False, use_reloader=False)
