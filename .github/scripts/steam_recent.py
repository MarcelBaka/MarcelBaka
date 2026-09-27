"""Generate a detailed Steam profile card without relying on image conversion."""

import base64
from datetime import datetime
from html import escape
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


OUTPUT = "metrics.steam.svg"
TIMEZONE = ZoneInfo("Europe/Warsaw")


def get_json(url, token=None):
    headers = {"Accept": "application/json", "User-Agent": "profile-metrics"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, headers=headers), timeout=15) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"API request failed (HTTP {error.code})") from None
    except (URLError, TimeoutError):
        raise RuntimeError("API request failed (network error)") from None


def steam_api(method, key, steam_id, **params):
    query = urlencode({"key": key, "steamid": steam_id, "format": "json", **params})
    return get_json(f"https://api.steampowered.com/{method}?" + query)


def optional_json(url):
    try:
        return get_json(url)
    except (RuntimeError, ValueError, KeyError, TypeError):
        return {}


def icon_data(game):
    """Accept only real images; Steam occasionally responds with non-image data."""
    digest = game.get("img_icon_url", "")
    if not re.fullmatch(r"[a-fA-F0-9]+", digest):
        return None
    url = f"https://media.steampowered.com/steamcommunity/public/images/apps/{int(game['appid'])}/{digest}.jpg"
    try:
        with urlopen(Request(url, headers={"User-Agent": "profile-metrics"}), timeout=8) as response:
            data = response.read(150_001)
        if len(data) > 150_000 or not data.startswith(b"\xff\xd8\xff"):
            return None
        return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
    except (HTTPError, URLError, TimeoutError):
        return None


def enrich(game, key, steam_id):
    game = dict(game)
    appid = int(game["appid"])
    details = optional_json(f"https://store.steampowered.com/api/appdetails?appids={appid}&l=en")
    about = details.get(str(appid), {})
    if isinstance(about, dict) and isinstance(about.get("data"), dict):
        game["genres"] = ", ".join(
            item.get("description", "") for item in about["data"].get("genres", [])
        )
    else:
        game["genres"] = ""
    game["icon"] = icon_data(game)
    try:
        result = steam_api(
            "ISteamUserStats/GetPlayerAchievements/v0001/", key, steam_id, appid=appid, l="en"
        )
        list_ = result.get("playerstats", {}).get("achievements", [])
        game["achievements"] = (sum(bool(item.get("achieved")) for item in list_), len(list_))
        game["latest_achievements"] = sorted(
            (item for item in list_ if item.get("achieved") and item.get("unlocktime")),
            key=lambda item: item["unlocktime"], reverse=True
        )[:2]
    except (RuntimeError, ValueError, TypeError, KeyError):
        game["achievements"] = None
        game["latest_achievements"] = []
    return game


def steam_data(key, steam_id):
    response = steam_api(
        "IPlayerService/GetOwnedGames/v0001/", key, steam_id, include_appinfo=1
    ).get("response", {})
    games = response.get("games")
    if games is None:
        raise RuntimeError("Steam did not return the games list; check profile privacy")
    summary_query = urlencode({"key": key, "steamids": steam_id, "format": "json"})
    player = get_json(
        "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?" + summary_query
    ).get("response", {}).get("players", [{}])[0]
    level = steam_api(
        "IPlayerService/GetSteamLevel/v1/", key, steam_id
    ).get("response", {}).get("player_level", "?")
    most = sorted(
        (game for game in games if game.get("playtime_forever", 0) >= 120),
        key=lambda game: game["playtime_forever"], reverse=True
    )[:3]
    recent = sorted(
        (game for game in games if game.get("rtime_last_played", 0)),
        key=lambda game: game["rtime_last_played"], reverse=True
    )[:3]
    details = {game["appid"]: enrich(game, key, steam_id) for game in most + recent}
    return {
        "name": player.get("personaname", "Steam player"),
        "level": level,
        "count": response.get("game_count", len(games)),
        "hours": sum(game.get("playtime_forever", 0) for game in games) / 60,
        "most": [details[game["appid"]] for game in most],
        "recent": [details[game["appid"]] for game in recent],
    }


def clean(value, limit):
    value = "".join(char for char in str(value) if ord(char) >= 32)
    return escape(value[:limit - 1] + "…" if len(value) > limit else value)


def date(timestamp):
    return datetime.fromtimestamp(timestamp, TIMEZONE).strftime("%d %b %Y")


def render(data):
    sections = [("Most played", data["most"]), ("Recently played", data["recent"])]
    height = 108 + sum(42 + 170 * max(len(games), 1) for _, games in sections)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="{height}" viewBox="0 0 480 {height}" role="img" aria-label="Steam profile and games">',
        '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}'
        '.bg{fill:#fff;stroke:#d0d7de}.title{fill:#24292f;font-size:16px;font-weight:600}'
        '.section{fill:#0969da;font-size:15px;font-weight:600}.name{fill:#0969da;font-size:14px;font-weight:600}'
        '.meta{fill:#57606a;font-size:12px}.rule{stroke:#d8dee4}.tile{fill:#eaeef2}'
        '@media(prefers-color-scheme:dark){.bg{fill:#0d1117;stroke:#30363d}'
        '.title{fill:#e6edf3}.section,.name{fill:#58a6ff}.meta{fill:#8b949e}'
        '.rule{stroke:#30363d}.tile{fill:#21262d}}</style>',
        f'<rect class="bg" x="0.5" y="0.5" width="479" height="{height - 1}" rx="6"/>',
        '<text class="title" x="20" y="31">Steam</text>',
        f'<text class="meta" x="20" y="56">{clean(data["name"], 44)} · Level {data["level"]}</text>',
        f'<text class="meta" x="20" y="78">{data["count"]} games · {data["hours"]:,.0f} hours played</text>',
    ]
    y = 108
    for heading, games in sections:
        parts.append(f'<text class="section" x="20" y="{y + 24}">{heading}</text>')
        y += 42
        if not games:
            parts.append(f'<text class="meta" x="20" y="{y + 28}">No games to show.</text>')
            y += 170
        for game in games:
            app_id = int(game["appid"])
            parts.append(f'<line class="rule" x1="20" x2="460" y1="{y}" y2="{y}"/>')
            if game["icon"]:
                parts.append(f'<image x="20" y="{y + 14}" width="32" height="32" href="{game["icon"]}"/>')
            else:
                parts.append(f'<rect class="tile" x="20" y="{y + 14}" width="32" height="32" rx="5"/>')
            parts.extend([
                f'<a href="https://store.steampowered.com/app/{app_id}/"><text class="name" x="62" y="{y + 29}">{clean(game.get("name", "Unknown game"), 46)}</text></a>',
                f'<text class="meta" x="62" y="{y + 48}">{clean(game["genres"], 55)}</text>',
                f'<text class="meta" x="62" y="{y + 70}">{game.get("playtime_forever", 0) / 60:,.1f} hours played</text>',
                f'<text class="meta" x="62" y="{y + 89}">Last played {date(game["rtime_last_played"]) if game.get("rtime_last_played") else "—"}</text>',
            ])
            achieved = game["achievements"]
            count = f'{achieved[0]} / {achieved[1]} achievements unlocked' if achieved else "Achievements unavailable"
            parts.append(f'<text class="meta" x="62" y="{y + 109}">{count}</text>')
            for index, item in enumerate(game["latest_achievements"]):
                name = item.get("name") or item.get("apiname") or "Achievement"
                parts.append(
                    f'<text class="meta" x="62" y="{y + 131 + index * 18}">✓ {clean(name, 46)} · {date(item["unlocktime"])}</text>'
                )
            y += 170
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def publish(svg, token, repository, branch):
    url = f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}/repos/{repository}/contents/{OUTPUT}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "profile-metrics",
    }
    current = get_json(url + "?" + urlencode({"ref": branch}), token)
    if base64.b64decode(current["content"]).decode("utf-8") == svg:
        print("Steam card is unchanged")
        return
    body = {
        "message": "Update detailed Steam profile and games",
        "branch": branch,
        "sha": current["sha"],
        "content": base64.b64encode(svg.encode("utf-8")).decode("ascii"),
    }
    request = Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="PUT")
    try:
        with urlopen(request, timeout=30) as response:
            response.read()
    except HTTPError as error:
        raise RuntimeError(f"Could not save Steam SVG (HTTP {error.code})") from None
    except (URLError, TimeoutError):
        raise RuntimeError("Could not save Steam SVG (network error)") from None
    print("Updated", OUTPUT)


if __name__ == "__main__":
    data = steam_data(os.environ["STEAM_API_KEY"], os.environ["STEAM_ID"])
    publish(render(data), os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_REF_NAME"])
