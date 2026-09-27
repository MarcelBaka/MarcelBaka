"""Render recent Steam games as an SVG without loading game artwork."""

import base64
from datetime import datetime
from html import escape
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


OUTPUT = "metrics.steam-recent.svg"


def get_json(url, token=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "profile-metrics"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"API request failed (HTTP {error.code})") from None
    except URLError:
        raise RuntimeError("API request failed (network error)") from None


def recent_games(key, steam_id):
    query = urlencode({"key": key, "steamid": steam_id, "format": "json", "include_appinfo": 1})
    url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/?" + query
    games = get_json(url).get("response", {}).get("games")
    if games is None:
        raise RuntimeError("Steam did not return the games list; check profile privacy")
    return sorted(
        (game for game in games if game.get("rtime_last_played", 0)),
        key=lambda game: game["rtime_last_played"],
        reverse=True,
    )[:3]


def render(games):
    height = 58 + 64 * max(len(games), 1)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="{height}" viewBox="0 0 480 {height}" role="img" aria-label="Recently played Steam games">',
        '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}'
        '.bg{fill:#fff;stroke:#d0d7de}.title{fill:#24292f;font-size:16px;font-weight:600}'
        '.name{fill:#0969da;font-size:14px;font-weight:600}.meta{fill:#57606a;font-size:12px}'
        '.rule{stroke:#d8dee4}'
        '@media(prefers-color-scheme:dark){.bg{fill:#0d1117;stroke:#30363d}'
        '.title{fill:#e6edf3}.name{fill:#58a6ff}.meta{fill:#8b949e}.rule{stroke:#30363d}}</style>',
        f'<rect class="bg" x="0.5" y="0.5" width="479" height="{height - 1}" rx="6"/>',
        '<text class="title" x="20" y="34">Recently played on Steam</text>',
    ]
    if not games:
        parts.append('<text class="meta" x="20" y="91">No played games to show.</text>')
    for index, game in enumerate(games):
        y = 58 + index * 64
        name = "".join(c for c in str(game.get("name", "Unknown game")) if ord(c) >= 32)
        if len(name) > 43:
            name = name[:42] + "…"
        played = datetime.fromtimestamp(game["rtime_last_played"], ZoneInfo("Europe/Warsaw"))
        hours = game.get("playtime_forever", 0) / 60
        app_id = int(game["appid"])
        parts.extend([
            f'<line class="rule" x1="20" x2="460" y1="{y}" y2="{y}"/>',
            f'<a href="https://store.steampowered.com/app/{app_id}/"><text class="name" x="20" y="{y + 24}">{escape(name)}</text></a>',
            f'<text class="meta" x="20" y="{y + 44}">Last played {played:%d %b %Y} · {hours:.1f} h total</text>',
        ])
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
    current = None
    try:
        current = get_json(url + "?" + urlencode({"ref": branch}), token)
    except RuntimeError as error:
        if "HTTP 404" not in str(error):
            raise
    if current and base64.b64decode(current["content"]).decode("utf-8") == svg:
        print("Recent Steam games are unchanged")
        return
    body = {
        "message": "Update recently played Steam games",
        "branch": branch,
        "content": base64.b64encode(svg.encode("utf-8")).decode("ascii"),
    }
    if current:
        body["sha"] = current["sha"]
    request = Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="PUT")
    try:
        with urlopen(request, timeout=30) as response:
            response.read()
    except HTTPError as error:
        raise RuntimeError(f"Could not save Steam SVG (HTTP {error.code})") from None
    except URLError:
        raise RuntimeError("Could not save Steam SVG (network error)") from None
    print("Updated", OUTPUT)


if __name__ == "__main__":
    games = recent_games(os.environ["STEAM_API_KEY"], os.environ["STEAM_ID"])
    publish(render(games), os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_REF_NAME"])
