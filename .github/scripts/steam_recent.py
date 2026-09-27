"""Generate a detailed Steam profile card without relying on image conversion."""

import base64
from datetime import datetime
from html import escape
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
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


def image_data(url):
    """Embed only actual images; Steam sometimes serves an error page instead."""
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    # Steam's achievement schema still supplies some image URLs over HTTP.
    # Upgrade only Steam-owned image hosts before fetching them.
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and (
        host == "steampowered.com" or host.endswith(".steampowered.com")
        or host == "steamstatic.com" or host.endswith(".steamstatic.com")
    ):
        url = urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    elif parsed.scheme != "https":
        return None
    try:
        with urlopen(Request(url, headers={"User-Agent": "profile-metrics"}), timeout=8) as response:
            data = response.read(150_001)
        if len(data) > 150_000:
            return None
        if data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            mime = "image/gif"
        else:
            return None
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    except (HTTPError, URLError, TimeoutError):
        return None


def icon_data(game, store_image=None):
    appid = int(game["appid"])
    for field in ("img_icon_url", "img_logo_url"):
        digest = game.get(field, "")
        if re.fullmatch(r"[a-fA-F0-9]+", digest):
            url = f"https://media.steampowered.com/steamcommunity/public/images/apps/{appid}/{digest}.jpg"
            icon = image_data(url)
            if icon:
                return icon
    return image_data(store_image)


def enrich(game, key, steam_id):
    game = dict(game)
    appid = int(game["appid"])
    details = optional_json(f"https://store.steampowered.com/api/appdetails?appids={appid}&l=en")
    about = details.get(str(appid), {})
    store_image = None
    if isinstance(about, dict) and isinstance(about.get("data"), dict):
        game["genres"] = ", ".join(
            item.get("description", "") for item in about["data"].get("genres", [])
        )
        store_image = about["data"].get("header_image")
    else:
        game["genres"] = ""
    game["icon"] = icon_data(game, store_image)
    try:
        schema_result = steam_api(
            "ISteamUserStats/GetSchemaForGame/v0002/", key, steam_id, appid=appid
        )
        schema_items = schema_result.get("game", {}).get("availableGameStats", {}).get("achievements", [])
        schema = {item["name"]: item for item in schema_items}
    except (RuntimeError, ValueError, TypeError, KeyError):
        schema = {}
    try:
        result = steam_api(
            "ISteamUserStats/GetPlayerAchievements/v0001/", key, steam_id, appid=appid, l="en"
        )
        list_ = result.get("playerstats", {}).get("achievements", [])
        game["achievements"] = (sum(bool(item.get("achieved")) for item in list_), len(list_))
        latest = sorted(
            (item for item in list_ if item.get("achieved") and item.get("unlocktime")),
            key=lambda item: item["unlocktime"], reverse=True
        )[:2]
        game["latest_achievements"] = [
            {
                **item,
                "icon": (
                    image_data(schema.get(item.get("apiname"), {}).get("icon"))
                    or image_data(schema.get(item.get("apiname"), {}).get("icongray"))
                ),
            }
            for item in latest
        ]
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


def embedded_image(source, x, y, size):
    return (
        f'<foreignObject x="{x}" y="{y}" width="{size}" height="{size}">'
        f'<img xmlns="http://www.w3.org/1999/xhtml" src="{source}" '
        f'width="{size}" height="{size}" style="border-radius:5px;object-fit:cover"/>'
        '</foreignObject>'
    )


def render(data):
    sections = [("Most played", data["most"]), ("Recently played", data["recent"])]
    def row_height(game):
        latest = len(game["latest_achievements"])
        more = bool(game["achievements"] and game["achievements"][0] > latest)
        return 114 + latest * 34 + (17 if more else 0)

    height = 92 + sum(36 + sum(map(row_height, games)) + (90 if not games else 0) for _, games in sections)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="{height}" viewBox="0 0 480 {height}" role="img" aria-label="Steam profile and games">',
        '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}'
        '.title,.section,.name{fill:#0969da;font-size:15px;font-weight:400}'
        '.section{font-size:16px}.name{font-size:14px}.meta{fill:#57606a;font-size:12px}'
        '.player{fill:#57606a;font-size:13px}.glyph{fill:#8c959f;font-size:15px}.tile{fill:#eaeef2}'
        '@media(prefers-color-scheme:dark){.title,.section,.name{fill:#58a6ff}'
        '.meta,.player{fill:#8b949e}.glyph{fill:#959da5}.tile{fill:#21262d}}</style>',
        '<text class="glyph" x="15" y="24">◉</text>',
        '<text class="title" x="37" y="24">Steam</text>',
        f'<text class="glyph" x="15" y="49">♙</text><text class="player" x="36" y="49">{clean(data["name"], 27)}</text>',
        f'<text class="glyph" x="250" y="49">☆</text><text class="player" x="271" y="49">Steam level {data["level"]}</text>',
        f'<text class="glyph" x="15" y="72">◇</text><text class="player" x="36" y="72">{data["count"]} games</text>',
        f'<text class="glyph" x="250" y="72">◷</text><text class="player" x="271" y="72">{data["hours"]:,.0f} hours played</text>',
    ]
    y = 92
    for heading, games in sections:
        parts.append(f'<text class="glyph" x="49" y="{y + 18}">☷</text>')
        parts.append(f'<text class="section" x="72" y="{y + 18}">{heading}</text>')
        y += 36
        if not games:
            parts.append(f'<text class="meta" x="72" y="{y + 22}">No games to show.</text>')
            y += 90
        for game in games:
            app_id = int(game["appid"])
            if game["icon"]:
                parts.append(embedded_image(game["icon"], 55, y + 2, 32))
            else:
                parts.append(f'<rect class="tile" x="55" y="{y + 2}" width="32" height="32" rx="5"/>')
            parts.extend([
                f'<a href="https://store.steampowered.com/app/{app_id}/"><text class="name" x="96" y="{y + 15}">{clean(game.get("name", "Unknown game"), 48)}</text></a>',
                f'<text class="meta" x="96" y="{y + 34}">{clean(game["genres"], 50)}</text>',
                f'<text class="meta" x="96" y="{y + 53}">◷ {game.get("playtime_forever", 0) / 60:,.0f} hours played</text>',
                f'<text class="meta" x="96" y="{y + 72}">▤ Last played on {date(game["rtime_last_played"]) if game.get("rtime_last_played") else "—"}</text>',
            ])
            achieved = game["achievements"]
            count = f'{achieved[0]} / {achieved[1]} achievements unlocked' if achieved else "Achievements unavailable"
            parts.append(f'<text class="meta" x="96" y="{y + 91}">♧ {count}</text>')
            for index, item in enumerate(game["latest_achievements"]):
                ay = y + 101 + index * 34
                if item.get("icon"):
                    parts.append(embedded_image(item["icon"], 110, ay, 22))
                else:
                    parts.append(f'<rect class="tile" x="110" y="{ay}" width="22" height="22" rx="4"/>')
                name = item.get("name") or item.get("apiname") or "Achievement"
                parts.extend([
                    f'<text class="name" x="140" y="{ay + 10}">{clean(name, 31)}</text>',
                    f'<text class="meta" x="457" y="{ay + 10}" text-anchor="end">{date(item["unlocktime"])}</text>',
                    f'<text class="meta" x="140" y="{ay + 26}">{clean(item.get("description", ""), 45)}</text>',
                ])
            latest_count = len(game["latest_achievements"])
            if achieved and achieved[0] > latest_count:
                more = achieved[0] - latest_count
                parts.append(f'<text class="meta" x="141" y="{y + 113 + latest_count * 34}">+{more} others...</text>')
            y += row_height(game)
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
