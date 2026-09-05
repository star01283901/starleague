import requests
from Rengar import Rengar

ICONS_URL = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/profile-icons.json"
ICONS_BASE = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/profile-icons/"


def fetch_all_profile_icons():
    response = requests.get(ICONS_URL, timeout=15)
    if response.status_code != 200:
        raise RuntimeError(f"Could not fetch profile icons (HTTP {response.status_code})")

    icons = []
    for entry in response.json():
        icon_id = entry.get("id")
        if icon_id is None:
            continue
        # iconPath looks like: /lol-game-data/assets/v1/profile-icons/123.jpg
        icon_path = entry.get("iconPath", "")
        # extract filename from path
        filename = icon_path.split("/")[-1].lower() if icon_path else f"{icon_id}.jpg"
        # build CDN url
        img_url = ICONS_BASE + filename
        icons.append({
            "id": icon_id,
            "url": img_url,
            "filename": filename,
        })

    # sort by id
    icons.sort(key=lambda x: x["id"])
    return icons


def change_profile_icon(icon_id, rengar=None):
    icon_id = int(icon_id)
    if icon_id < 1:
        raise ValueError("Icon ID must be a positive number")

    api = rengar or Rengar()
    response = api.lcu_request(
        "PUT", "/lol-summoner/v1/current-summoner/icon", {"profileIconId": icon_id}
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Could not change profile icon (HTTP {response.status_code})")
    return icon_id
