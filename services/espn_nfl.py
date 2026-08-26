"""ESPN public NFL scoreboard client — finals only, no API key.

Used by the morning-after score beta. Never returns in-progress scores.
"""

import json
import urllib.error
import urllib.request
from datetime import date
from typing import Optional

from config import logger

ESPN_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
)
REQUEST_TIMEOUT_SECONDS = 10

# Canned final used by founder dry-run / preseason fake mornings.
FAKE_GAME = {
    "game_id": "fake-bengals-chiefs",
    "away_abbr": "CIN",
    "away_short": "Bengals",
    "away_score": "27",
    "home_abbr": "KC",
    "home_short": "Chiefs",
    "home_score": "24",
}


def _http_get_json(url: str) -> Optional[dict]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "RemyndrsScoreBeta/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as e:
        logger.warning(f"ESPN scoreboard fetch failed: {e}")
        return None


def fetch_scoreboard(game_date: date, fetch_fn=_http_get_json) -> Optional[dict]:
    """Fetch ESPN scoreboard JSON for a YYYYMMDD date."""
    dates = game_date.strftime("%Y%m%d")
    url = f"{ESPN_SCOREBOARD_URL}?dates={dates}"
    return fetch_fn(url)


def _competitor_fields(competitor: dict) -> dict:
    team = competitor.get("team") or {}
    short = (
        team.get("shortDisplayName")
        or team.get("name")
        or team.get("abbreviation")
        or "Team"
    )
    return {
        "abbr": (team.get("abbreviation") or "").upper(),
        "short": short,
        "score": str(competitor.get("score") or "0"),
        "home_away": competitor.get("homeAway"),
    }


def parse_finals(scoreboard: Optional[dict]) -> list[dict]:
    """Return completed NFL games only (STATUS_FINAL / completed=true)."""
    if not scoreboard:
        return []

    finals = []
    for event in scoreboard.get("events") or []:
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competition = competitions[0]
        status = (competition.get("status") or {}).get("type") or {}
        completed = bool(status.get("completed")) or (status.get("name") == "STATUS_FINAL")
        if not completed:
            continue

        competitors = competition.get("competitors") or []
        home = away = None
        for c in competitors:
            fields = _competitor_fields(c)
            if fields["home_away"] == "home":
                home = fields
            elif fields["home_away"] == "away":
                away = fields

        if not home or not away:
            continue

        finals.append({
            "game_id": str(event.get("id") or competition.get("id") or ""),
            "away_abbr": away["abbr"],
            "away_short": away["short"],
            "away_score": away["score"],
            "home_abbr": home["abbr"],
            "home_short": home["short"],
            "home_score": home["score"],
        })
    return finals


def find_team_final(finals: list[dict], team_abbr: str) -> Optional[dict]:
    """Return the final involving team_abbr, or None."""
    want = (team_abbr or "").upper()
    for game in finals:
        if game["away_abbr"] == want or game["home_abbr"] == want:
            return game
    return None


def format_final(game: dict) -> str:
    """Locked SCORE reply format: 'Bengals 27, Chiefs 24' (away, home)."""
    return (
        f"{game['away_short']} {game['away_score']}, "
        f"{game['home_short']} {game['home_score']}"
    )


def fake_game_for_team(team_abbr: str, team_short: str) -> dict:
    """Canned final for founder dry-run. Keeps the user's team in the box score."""
    abbr = (team_abbr or "CIN").upper()
    short = team_short or "Bengals"
    if abbr == "CIN":
        return dict(FAKE_GAME)
    if abbr == "KC":
        return {
            "game_id": "fake-chiefs-bengals",
            "away_abbr": "KC",
            "away_short": "Chiefs",
            "away_score": "24",
            "home_abbr": "CIN",
            "home_short": "Bengals",
            "home_score": "27",
        }
    return {
        "game_id": f"fake-{abbr.lower()}-kc",
        "away_abbr": abbr,
        "away_short": short,
        "away_score": "27",
        "home_abbr": "KC",
        "home_short": "Chiefs",
        "home_score": "24",
    }
