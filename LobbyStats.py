"""
LobbyStats.py
Fetches op.gg stats for each teammate during champion select.
Used by the Auto Lobby Reveal feature in star.
Uses direct op.gg API calls — no third-party opgg.py dependency needed.
"""

import asyncio
import threading
import logging
import requests
from dataclasses import dataclass, field
from typing import Optional

from Rengar import Rengar

log = logging.getLogger(__name__)

# ── Region mapping: LCU webRegion → op.gg region string ──────────────────────
REGION_MAP = {
    "br":   "br",
    "eune": "eune",
    "euw":  "euw",
    "jp":   "jp",
    "kr":   "kr",
    "lan":  "lan",
    "las":  "las",
    "na":   "na",
    "oce":  "oce",
    "ru":   "ru",
    "tr":   "tr",
    "ph":   "ph",
    "sg":   "sg",
    "th":   "th",
    "tw":   "tw",
    "vn":   "vn",
}

OPGG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://op.gg/",
    "Origin": "https://op.gg",
}

SESSION = requests.Session()
SESSION.headers.update(OPGG_HEADERS)


@dataclass
class PlayerStats:
    game_name:    str = ""
    tag_line:     str = ""
    level:        int = 0
    rank:         str = "Unranked"
    lp:           int = 0
    wins:         int = 0
    losses:       int = 0
    winrate:      float = 0.0
    games:        int = 0
    recent_wr:    Optional[float] = None
    top_champs:   list = field(default_factory=list)
    error:        Optional[str] = None


def _safe_int(val, default=0) -> int:
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _safe_float(val, default=0.0) -> float:
    try:
        v = str(val).replace("%", "").strip()
        return float(v) if v else default
    except (TypeError, ValueError):
        return default


_champ_id_to_name: dict = {}
_champ_name_cache_lock = threading.Lock()


def _get_champ_id_map() -> dict:
    """Returns {champion_key_str: champion_name} e.g. {'1': 'Annie'}"""
    global _champ_id_to_name
    with _champ_name_cache_lock:
        if _champ_id_to_name:
            return _champ_id_to_name
        try:
            r = SESSION.get(
                "https://ddragon.leagueoflegends.com/api/versions.json", timeout=5
            )
            patch = r.json()[0]
            url = f"https://ddragon.leagueoflegends.com/cdn/{patch}/data/en_US/champion.json"
            r2 = SESSION.get(url, timeout=8)
            data = r2.json()["data"]
            _champ_id_to_name = {v["key"]: v["id"] for v in data.values()}
        except Exception as e:
            log.warning("Failed to load champion map: %s", e)
            _champ_id_to_name = {}
        return _champ_id_to_name


def _fetch_opgg_summoner(game_name: str, tag: str, region: str) -> dict:
    """
    Calls op.gg internal API to get summoner profile data.
    Returns the raw summoner JSON dict or raises.
    """
    url = (
        f"https://op.gg/api/v1.0/internal/bypass/summoners/{region}/summoners"
        f"?gameName={requests.utils.quote(game_name)}&tagLine={requests.utils.quote(tag)}"
    )
    r = SESSION.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    # op.gg returns {"data": [...]} or {"data": {...}}
    body = data.get("data", data)
    if isinstance(body, list):
        return body[0] if body else {}
    return body


def _fetch_opgg_stats(summoner_id: str, region: str) -> dict:
    """
    Fetches ranked stats for a summoner_id from op.gg.
    Returns {"league_stats": [...], "most_champions": [...]}
    """
    url = f"https://op.gg/api/v1.0/internal/bypass/summoners/{region}/{summoner_id}/summary"
    r = SESSION.get(url, timeout=10)
    r.raise_for_status()
    return r.json().get("data", {})


def _fetch_stats_sync(summoner_name: str, tag: str, region: str) -> PlayerStats:
    """Fetches a player's op.gg stats synchronously. Safe to call from a thread."""
    stats = PlayerStats(game_name=summoner_name, tag_line=tag)
    try:
        # Step 1: resolve summoner
        profile = _fetch_opgg_summoner(summoner_name, tag, region)
        if not profile:
            stats.error = "Player not found"
            return stats

        # Level — op.gg puts it at profile["summoner"]["level"] or profile["level"]
        summoner_obj = profile.get("summoner") or profile
        stats.level = _safe_int(
            summoner_obj.get("level")
            or summoner_obj.get("summoner_level")
            or profile.get("level", 0)
        )

        summoner_id = (
            profile.get("summoner_id")
            or (profile.get("summoner") or {}).get("id")
            or profile.get("id", "")
        )

        if not summoner_id:
            # No ID — can't fetch ranked stats but we have the name at least
            return stats

        # Step 2: fetch ranked/stat summary
        summary = _fetch_opgg_stats(str(summoner_id), region)

        # Ranked stats — op.gg returns list under "league_stats"
        for ls in summary.get("league_stats", []):
            queue = str(ls.get("queue_info", {}).get("game_type", "") or ls.get("game_type", "")).upper()
            if "RANKED_SOLO" not in queue and "SOLO" not in queue:
                continue
            tier_info = ls.get("tier_info", {}) or {}
            tier     = tier_info.get("tier", "") or ""
            division = tier_info.get("division", "") or ""
            stats.rank = f"{tier} {division}".strip() if tier else "Unranked"
            stats.lp   = _safe_int(tier_info.get("lp", 0))
            stats.wins   = _safe_int(ls.get("win", 0))
            stats.losses = _safe_int(ls.get("lose", 0))
            stats.games  = stats.wins + stats.losses
            stats.winrate = round(stats.wins / stats.games * 100, 1) if stats.games else 0.0
            break

        # Most played champions
        champ_map = _get_champ_id_map()
        champ_names = []
        for c in summary.get("most_champions", [])[:3]:
            cid  = str(c.get("champion_id", ""))
            name = champ_map.get(cid) or c.get("champion_name") or f"#{cid}"
            champ_names.append(name)
        stats.top_champs = champ_names

    except requests.HTTPError as e:
        stats.error = f"HTTP {e.response.status_code}" if e.response is not None else str(e)
    except requests.ConnectionError:
        stats.error = "Network error — check internet connection"
    except requests.Timeout:
        stats.error = "Timeout"
    except Exception as e:
        log.exception("Error fetching stats for %s#%s", summoner_name, tag)
        stats.error = str(e)

    return stats


def get_lobby_stats(rengar: Optional[Rengar] = None) -> dict:
    """
    Entry point called from app.py.
    Returns a dict with 'region' and 'players' list, ready to JSON-serialize.
    Raises RuntimeError if not in champ select or can't read names.
    """
    api = rengar or Rengar()

    # ── Get champ select session ──────────────────────────────────────────────
    cs = api.lcu_request("GET", "/lol-champ-select/v1/session", "")
    if cs.status_code != 200 or "RPC_ERROR" in cs.text:
        raise RuntimeError("Auto Lobby Reveal is only available during champion select")

    session = cs.json()

    # ── Get region ────────────────────────────────────────────────────────────
    region_resp = api.lcu_request("GET", "/riotclient/region-locale", "")
    lcu_region  = ""
    if region_resp.status_code == 200:
        lcu_region = region_resp.json().get("webRegion", "").lower()
    region = REGION_MAP.get(lcu_region, "euw")

    # ── Collect summoner names ────────────────────────────────────────────────
    summoners = []  # list of (game_name, tag)

    hidden_names = any(
        p.get("nameVisibilityType") == "HIDDEN"
        for p in session.get("myTeam", [])
    )

    if hidden_names:
        participants = api.riot_request("GET", "/chat/v5/participants", "")
        for p in participants.json().get("participants", []):
            if "champ-select" in p.get("cid", ""):
                summoners.append((p.get("game_name", ""), p.get("game_tag", "")))
    else:
        for player in session.get("myTeam", []):
            sid = player.get("summonerId")
            if not sid or sid == "0" or sid == 0:
                continue
            r = api.lcu_request("GET", f"/lol-summoner/v1/summoners/{sid}", "")
            if r.status_code == 200:
                s = r.json()
                summoners.append((s.get("gameName", ""), s.get("tagLine", "")))

    if not summoners:
        raise RuntimeError("Could not read summoner names from lobby")

    # ── Fetch stats in parallel threads ──────────────────────────────────────
    results: list[Optional[PlayerStats]] = [None] * len(summoners)

    def fetch(idx, name, tag):
        results[idx] = _fetch_stats_sync(name, tag, region)

    threads = [
        threading.Thread(target=fetch, args=(i, name, tag), daemon=True)
        for i, (name, tag) in enumerate(summoners)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    players = []
    for i, ps in enumerate(results):
        if ps is None:
            name, tag = summoners[i]
            ps = PlayerStats(game_name=name, tag_line=tag, error="Timeout")
        players.append({
            "game_name":  ps.game_name,
            "tag_line":   ps.tag_line,
            "level":      ps.level,
            "rank":       ps.rank,
            "lp":         ps.lp,
            "wins":       ps.wins,
            "losses":     ps.losses,
            "winrate":    ps.winrate,
            "games":      ps.games,
            "recent_wr":  ps.recent_wr,
            "top_champs": ps.top_champs,
            "error":      ps.error,
        })

    return {"region": lcu_region.upper(), "players": players}
