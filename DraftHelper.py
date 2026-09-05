"""
DraftHelper.py
Draft helper for star — suggests comfort picks and best picks
based on Lolalytics data + your LCU champion mastery.

axe.lolalytics.com is the real Lolalytics API backend.
Patch format is dot-separated (16.11), NOT underscore.
Lolalytics typically lags DDragon by several patches — probe backwards.
"""

import threading
import requests
import time
import logging
from typing import Optional

from Rengar import Rengar

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_ICON_BASE    = "https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{name}.png"

# axe. is the real API backend — lolalytics.com/api/ is blocked/dead
LOLALYTICS_BASE = "https://axe.lolalytics.com/mega/"

COMFORT_MIN_POINTS = 30_000
COMFORT_MIN_LEVEL  = 4
TOP_N              = 5

LANE_MAP = {
    "TOP":     "top",
    "JUNGLE":  "jungle",
    "MIDDLE":  "mid",
    "BOTTOM":  "bot",      # Lolalytics uses "bot" not "adc"
    "UTILITY": "support",
}

LOLA_TIER_MAP = {
    "emerald": "emerald_plus",
    "master":  "master_plus",
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin":          "https://lolalytics.com",
    "Referer":         "https://lolalytics.com/",
})

# ── Data Dragon helpers ────────────────────────────────────────────────────────

_patch_cache:      Optional[str]  = None
_champ_key_cache:  Optional[dict] = None
_champ_name_cache: Optional[dict] = None
_lola_patch_cache: Optional[str]  = None


def _get_patch() -> str:
    global _patch_cache
    if _patch_cache:
        return _patch_cache
    try:
        r = SESSION.get(DDRAGON_VERSIONS_URL, timeout=5)
        _patch_cache = r.json()[0]
    except Exception:
        _patch_cache = "16.11.1"
    return _patch_cache


def _get_champ_map() -> dict:
    """Returns {champion_key_str: DDragon_id} e.g. {'1': 'Annie'}"""
    global _champ_key_cache
    if _champ_key_cache:
        return _champ_key_cache
    try:
        patch = _get_patch()
        url = f"https://ddragon.leagueoflegends.com/cdn/{patch}/data/en_US/champion.json"
        r = SESSION.get(url, timeout=8)
        data = r.json()["data"]
        _champ_key_cache = {v["key"]: v["id"] for v in data.values()}
    except Exception:
        _champ_key_cache = {}
    return _champ_key_cache


def _get_display_name_map() -> dict:
    """Returns {DDragon_id: display_name} e.g. {'MissFortune': 'Miss Fortune'}"""
    global _champ_name_cache
    if _champ_name_cache:
        return _champ_name_cache
    try:
        patch = _get_patch()
        url = f"https://ddragon.leagueoflegends.com/cdn/{patch}/data/en_US/champion.json"
        r = SESSION.get(url, timeout=8)
        data = r.json()["data"]
        _champ_name_cache = {v["id"]: v["name"] for v in data.values()}
    except Exception:
        _champ_name_cache = {}
    return _champ_name_cache


def _probe_lola_patch(major: int, minor: int) -> Optional[str]:
    """
    Probe axe.lolalytics.com backwards from (major, minor) until we hit
    a patch that has real data. Lolalytics typically lags DDragon by
    several patches so we walk up to 10 back.
    Patch format is dot-separated: "16.11", NOT "16_11".
    """
    for delta in range(11):
        m = minor - delta
        if m < 1:
            break
        patch_str = f"{major}.{m}"
        url = (
            f"{LOLALYTICS_BASE}"
            f"?ep=tierlist&p=d&patch={patch_str}"
            f"&tier=emerald_plus&queue=420&region=all&lane=mid"
        )
        try:
            r = SESSION.get(url, timeout=8)
            if r.status_code == 200:
                data = r.json()
                # Real data has champion keys like "1", "2" etc + metadata like "header"
                champ_entries = [k for k in data if k.isdigit()]
                if len(champ_entries) > 50:
                    log.info("Lolalytics patch found: %s", patch_str)
                    return patch_str
        except Exception as e:
            log.debug("Patch probe %s error: %s", patch_str, e)
    return None


def _get_lola_patch() -> str:
    """
    Returns the latest patch string lolalytics actually serves.
    Probes backwards from DDragon current patch.
    Never sets the cache to a patch that failed — if all probes fail,
    returns a best-guess without caching so next call tries again.
    """
    global _lola_patch_cache
    if _lola_patch_cache:
        return _lola_patch_cache
    try:
        patch = _get_patch()          # e.g. "16.17.1"
        parts = patch.split(".")
        major = int(parts[0])
        minor = int(parts[1])
        result = _probe_lola_patch(major, minor)
        if result:
            _lola_patch_cache = result
            return _lola_patch_cache
        # All probes failed (network issue?). Return best guess WITHOUT caching
        # so the next request tries again rather than hammering a bad value.
        log.warning("All Lolalytics patch probes failed — using best-guess %d.%d", major, minor - 6)
        return f"{major}.{max(1, minor - 6)}"
    except Exception:
        return "16.11"


def invalidate_lola_patch():
    global _lola_patch_cache
    _lola_patch_cache = None


def champ_icon_url(champ_id: str) -> str:
    patch = _get_patch()
    FIX = {"Wukong": "MonkeyKing", "NunuWillump": "Nunu"}
    name = FIX.get(champ_id, champ_id)
    return DDRAGON_ICON_BASE.format(version=patch, name=name)


# ── LCU helpers ────────────────────────────────────────────────────────────────

def _get_my_mastery(api: Rengar) -> dict:
    champ_map = _get_champ_map()
    result = {}
    try:
        r = api.lcu_request("GET", "/lol-champion-mastery/v1/local-player/champion-mastery", "")
        if r.status_code != 200:
            return result
        for entry in r.json():
            cid    = str(entry.get("championId", ""))
            points = entry.get("championPoints", 0)
            level  = entry.get("championLevel", 0)
            name   = champ_map.get(cid)
            if not name:
                continue
            if level >= COMFORT_MIN_LEVEL or points >= COMFORT_MIN_POINTS:
                result[name] = {"points": points, "level": level}
    except Exception:
        pass
    return result


def _get_my_position(api: Rengar) -> str:
    try:
        r = api.lcu_request("GET", "/lol-champ-select/v1/session", "")
        if r.status_code != 200:
            return "mid"
        session  = r.json()
        local_id = session.get("localPlayerCellId", -1)
        for player in session.get("myTeam", []):
            if player.get("cellId") == local_id:
                pos = player.get("assignedPosition", "").upper()
                return LANE_MAP.get(pos, "mid")
    except Exception:
        pass
    return "mid"


def _resolve_champ(player: dict, champ_map: dict) -> Optional[str]:
    cid = str(player.get("championId", 0))
    if not cid or cid == "0":
        cid = str(player.get("championPickIntent", 0))
    if cid and cid != "0":
        return champ_map.get(cid)
    return None


def _get_session_picks(api: Rengar):
    """
    Returns (my_pick, enemy_picks, ally_picks, is_my_turn, my_action_type).
    is_my_turn = True when the local player has an in-progress action.
    my_action_type = 'pick' | 'ban' | None
    """
    champ_map = _get_champ_map()
    my_pick        = None
    enemy_picks    = []
    ally_picks     = []
    is_my_turn     = False
    my_action_type = None

    try:
        r = api.lcu_request("GET", "/lol-champ-select/v1/session", "")
        if r.status_code != 200:
            return my_pick, enemy_picks, ally_picks, is_my_turn, my_action_type

        session  = r.json()
        local_id = session.get("localPlayerCellId", -1)

        for phase in session.get("actions", []):
            for action in phase:
                if action.get("actorCellId") == local_id and action.get("isInProgress", False):
                    is_my_turn     = True
                    my_action_type = action.get("type")  # 'pick' or 'ban'
                    break

        for player in session.get("myTeam", []):
            name = _resolve_champ(player, champ_map)
            if not name:
                continue
            if player.get("cellId") == local_id:
                my_pick = name
            else:
                ally_picks.append(name)

        for player in session.get("theirTeam", []):
            name = _resolve_champ(player, champ_map)
            if name:
                enemy_picks.append(name)

    except Exception:
        pass
    return my_pick, enemy_picks, ally_picks, is_my_turn, my_action_type


# ── Lolalytics API (axe.lolalytics.com) ──────────────────────────────────────

def _lola_tierlist(lane: str, tier: str) -> list[dict]:
    """
    Fetches the tier list from axe.lolalytics.com.
    Returns list of {champion_id, winrate, tier, games} sorted by winrate desc.
    champion_id here is the numeric string key Lolalytics uses (matches DDragon key).
    """
    patch      = _get_lola_patch()
    tier_param = LOLA_TIER_MAP.get(tier, "emerald_plus")
    url = (
        f"{LOLALYTICS_BASE}"
        f"?ep=tierlist&p=d&patch={patch}"
        f"&tier={tier_param}&queue=420&region=all&lane={lane}"
    )
    try:
        r = SESSION.get(url, timeout=12)
        if r.status_code != 200:
            log.warning("Lolalytics tierlist %s: %s %s", patch, r.status_code, url)
            invalidate_lola_patch()
            return []
        data = r.json()

        # axe. response: keys are champion numeric IDs, plus a "header" metadata key
        # Each champion value is a list: [rank, winrate, pickrate, banrate, games, tier_str, ...]
        # Layout confirmed from community reverse engineering of the endpoint.
        champ_map = _get_champ_map()   # numeric_str → DDragon id
        results   = []

        for key, val in data.items():
            if not key.isdigit():
                continue  # skip "header" and other metadata
            ddragon_id = champ_map.get(key)
            if not ddragon_id:
                continue
            try:
                if isinstance(val, (list, tuple)) and len(val) >= 5:
                    wr    = float(val[1])   # index 1 = winrate
                    games = int(val[4])     # index 4 = games
                    tier_s = str(val[5]) if len(val) > 5 else ""
                elif isinstance(val, dict):
                    wr    = float(val.get("wr", 0) or val.get("winrate", 0))
                    games = int(val.get("n", 0)  or val.get("games", 0))
                    tier_s = val.get("tier", "")
                else:
                    continue
                if games < 100:
                    continue
                results.append({
                    "champion_id": ddragon_id,
                    "winrate":     wr,
                    "tier":        tier_s,
                    "games":       games,
                })
            except (ValueError, TypeError, IndexError):
                continue

        results.sort(key=lambda x: x["winrate"], reverse=True)
        log.info("Lolalytics tierlist OK: patch=%s lane=%s champions=%d", patch, lane, len(results))
        return results

    except Exception as e:
        log.warning("Lolalytics tierlist failed: %s", e)
        return []


def _lola_matchup_wr(champ_id: str, enemy_id: str, lane: str, tier: str) -> Optional[float]:
    """
    Returns winrate of champ_id vs enemy_id.
    champ_id / enemy_id are DDragon ids — we need their numeric keys for axe.
    """
    # Build reverse map: DDragon id → numeric key
    champ_map = _get_champ_map()
    rev_map   = {v: k for k, v in champ_map.items()}

    c_key = rev_map.get(champ_id)
    o_key = rev_map.get(enemy_id)
    if not c_key or not o_key:
        return None

    patch      = _get_lola_patch()
    tier_param = LOLA_TIER_MAP.get(tier, "emerald_plus")
    url = (
        f"{LOLALYTICS_BASE}"
        f"?ep=champion2&p=d&patch={patch}"
        f"&tier={tier_param}&queue=420&region=all&lane={lane}"
        f"&c={c_key}&o={o_key}"
    )
    try:
        r = SESSION.get(url, timeout=8)
        if r.status_code != 200:
            return None
        data = r.json()
        # Response: {"header": {...}, "vs": [...]} or {"header": {...}, "matchup": {...}}
        vs = data.get("vs") or data.get("matchup")
        if isinstance(vs, list) and len(vs) >= 2:
            return float(vs[1])   # index 1 = winrate
        if isinstance(vs, dict):
            wr = vs.get("wr") or vs.get("winrate")
            if wr is not None:
                return float(wr)
    except Exception:
        pass
    return None


# ── Core scoring ──────────────────────────────────────────────────────────────

def _score_entry(champ_id: str, tier_wr: float, enemy_picks: list[str], lane: str, tier: str) -> float:
    if not enemy_picks:
        return tier_wr
    matchup_scores = []
    for enemy in enemy_picks:
        wr = _lola_matchup_wr(champ_id, enemy, lane, tier)
        if wr is not None:
            matchup_scores.append(wr)
    if not matchup_scores:
        return tier_wr
    avg_matchup = sum(matchup_scores) / len(matchup_scores)
    return tier_wr * 0.4 + avg_matchup * 0.6


def get_draft_suggestions(rank_key: str = "emerald", rengar: Optional[Rengar] = None) -> dict:
    api = rengar or Rengar()
    display_names = _get_display_name_map()

    try:
        lane                                                 = _get_my_position(api)
        my_pick, enemy_picks, ally_picks, is_my_turn, my_action_type = _get_session_picks(api)
        mastery                                              = _get_my_mastery(api)

        tierlist = _lola_tierlist(lane, rank_key)

        if not tierlist:
            return {
                "lane":           lane,
                "rank":           rank_key,
                "my_pick":        display_names.get(my_pick, my_pick) if my_pick else None,
                "enemy_picks":    [display_names.get(e, e) for e in enemy_picks],
                "ally_picks":     [display_names.get(a, a) for a in ally_picks],
                "is_my_turn":     is_my_turn,
                "my_action_type": my_action_type,
                "comfort_pick":   None,
                "best_pick":      None,
                "comfort_list":   [],
                "best_list":      [],
                "error":          "Could not load Lolalytics data — check internet",
            }

        scored = []
        lock   = threading.Lock()

        def score_entry(entry):
            cid = entry["champion_id"]
            sc  = _score_entry(cid, entry["winrate"], enemy_picks, lane, rank_key)
            display = display_names.get(cid, cid)
            row = {
                "champion":      display,
                "champion_id":   cid,
                "winrate":       entry["winrate"],
                "tier":          entry["tier"],
                "counter_score": round(sc, 2),
                "icon_url":      champ_icon_url(cid),
            }
            with lock:
                scored.append(row)

        threads = [threading.Thread(target=score_entry, args=(e,), daemon=True) for e in tierlist]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=12)

        scored.sort(key=lambda x: x["counter_score"], reverse=True)

        comfort_scored = []
        for entry in scored:
            cid = entry["champion_id"]
            if cid in mastery:
                comfort_scored.append({
                    **entry,
                    "mastery_points": mastery[cid]["points"],
                    "mastery_level":  mastery[cid]["level"],
                })

        my_pick_display = display_names.get(my_pick, my_pick) if my_pick else None
        enemy_display   = [display_names.get(e, e) for e in enemy_picks]
        ally_display    = [display_names.get(a, a) for a in ally_picks]

        return {
            "lane":           lane,
            "rank":           rank_key,
            "my_pick":        my_pick_display,
            "enemy_picks":    enemy_display,
            "ally_picks":     ally_display,
            "is_my_turn":     is_my_turn,
            "my_action_type": my_action_type,
            "comfort_pick":   comfort_scored[0] if comfort_scored else None,
            "best_pick":      scored[0] if scored else None,
            "comfort_list":   comfort_scored[:TOP_N],
            "best_list":      scored[:TOP_N],
            "error":          None,
        }

    except Exception as e:
        log.exception("get_draft_suggestions failed")
        return {
            "lane":           "",
            "rank":           rank_key,
            "my_pick":        None,
            "enemy_picks":    [],
            "ally_picks":     [],
            "is_my_turn":     False,
            "my_action_type": None,
            "comfort_pick":   None,
            "best_pick":      None,
            "comfort_list":   [],
            "best_list":      [],
            "error":          str(e),
        }
