"""NFL team alias map for the morning-after score beta.

Users may text YES + Bengals, YES + Cincinnati, YES + CIN, etc.
One team per user; aliases collapse to a canonical ESPN abbreviation.
"""

from typing import Optional


# Canonical record: abbreviation (ESPN), short display name, full name, aliases.
# Short name is what we put in SMS: "{Team} played last night..."
NFL_TEAMS = {
    "ARI": {
        "abbr": "ARI",
        "short": "Cardinals",
        "full": "Arizona Cardinals",
        "aliases": ["arizona", "arizona cardinals", "cardinals", "cards", "ari", "az"],
    },
    "ATL": {
        "abbr": "ATL",
        "short": "Falcons",
        "full": "Atlanta Falcons",
        "aliases": ["atlanta", "atlanta falcons", "falcons", "atl"],
    },
    "BAL": {
        "abbr": "BAL",
        "short": "Ravens",
        "full": "Baltimore Ravens",
        "aliases": ["baltimore", "baltimore ravens", "ravens", "bal"],
    },
    "BUF": {
        "abbr": "BUF",
        "short": "Bills",
        "full": "Buffalo Bills",
        "aliases": ["buffalo", "buffalo bills", "bills", "buf"],
    },
    "CAR": {
        "abbr": "CAR",
        "short": "Panthers",
        "full": "Carolina Panthers",
        "aliases": ["carolina", "carolina panthers", "panthers", "car"],
    },
    "CHI": {
        "abbr": "CHI",
        "short": "Bears",
        "full": "Chicago Bears",
        "aliases": ["chicago", "chicago bears", "bears", "chi"],
    },
    "CIN": {
        "abbr": "CIN",
        "short": "Bengals",
        "full": "Cincinnati Bengals",
        "aliases": ["cincinnati", "cincinnati bengals", "bengals", "cin", "cincy"],
    },
    "CLE": {
        "abbr": "CLE",
        "short": "Browns",
        "full": "Cleveland Browns",
        "aliases": ["cleveland", "cleveland browns", "browns", "cle"],
    },
    "DAL": {
        "abbr": "DAL",
        "short": "Cowboys",
        "full": "Dallas Cowboys",
        "aliases": ["dallas", "dallas cowboys", "cowboys", "dal"],
    },
    "DEN": {
        "abbr": "DEN",
        "short": "Broncos",
        "full": "Denver Broncos",
        "aliases": ["denver", "denver broncos", "broncos", "den"],
    },
    "DET": {
        "abbr": "DET",
        "short": "Lions",
        "full": "Detroit Lions",
        "aliases": ["detroit", "detroit lions", "lions", "det"],
    },
    "GB": {
        "abbr": "GB",
        "short": "Packers",
        "full": "Green Bay Packers",
        "aliases": ["green bay", "green bay packers", "packers", "gb", "pack"],
    },
    "HOU": {
        "abbr": "HOU",
        "short": "Texans",
        "full": "Houston Texans",
        "aliases": ["houston", "houston texans", "texans", "hou"],
    },
    "IND": {
        "abbr": "IND",
        "short": "Colts",
        "full": "Indianapolis Colts",
        "aliases": ["indianapolis", "indianapolis colts", "colts", "ind"],
    },
    "JAX": {
        "abbr": "JAX",
        "short": "Jaguars",
        "full": "Jacksonville Jaguars",
        "aliases": ["jacksonville", "jacksonville jaguars", "jaguars", "jags", "jax", "jac"],
    },
    "KC": {
        "abbr": "KC",
        "short": "Chiefs",
        "full": "Kansas City Chiefs",
        "aliases": ["kansas city", "kansas city chiefs", "chiefs", "kc"],
    },
    "LV": {
        "abbr": "LV",
        "short": "Raiders",
        "full": "Las Vegas Raiders",
        "aliases": ["las vegas", "las vegas raiders", "raiders", "lv", "lvr", "oakland", "oakland raiders"],
    },
    "LAC": {
        "abbr": "LAC",
        "short": "Chargers",
        "full": "Los Angeles Chargers",
        "aliases": ["chargers", "la chargers", "los angeles chargers", "lac", "san diego", "san diego chargers", "sd"],
    },
    "LAR": {
        "abbr": "LAR",
        "short": "Rams",
        "full": "Los Angeles Rams",
        "aliases": ["rams", "la rams", "los angeles rams", "lar", "st louis", "st louis rams", "stl"],
    },
    "MIA": {
        "abbr": "MIA",
        "short": "Dolphins",
        "full": "Miami Dolphins",
        "aliases": ["miami", "miami dolphins", "dolphins", "mia", "phins"],
    },
    "MIN": {
        "abbr": "MIN",
        "short": "Vikings",
        "full": "Minnesota Vikings",
        "aliases": ["minnesota", "minnesota vikings", "vikings", "min", "vikes"],
    },
    "NE": {
        "abbr": "NE",
        "short": "Patriots",
        "full": "New England Patriots",
        "aliases": ["new england", "new england patriots", "patriots", "pats", "ne"],
    },
    "NO": {
        "abbr": "NO",
        "short": "Saints",
        "full": "New Orleans Saints",
        "aliases": ["new orleans", "new orleans saints", "saints", "no", "nola"],
    },
    "NYG": {
        "abbr": "NYG",
        "short": "Giants",
        "full": "New York Giants",
        "aliases": ["giants", "ny giants", "new york giants", "nyg"],
    },
    "NYJ": {
        "abbr": "NYJ",
        "short": "Jets",
        "full": "New York Jets",
        "aliases": ["jets", "ny jets", "new york jets", "nyj"],
    },
    "PHI": {
        "abbr": "PHI",
        "short": "Eagles",
        "full": "Philadelphia Eagles",
        "aliases": ["philadelphia", "philadelphia eagles", "eagles", "phi", "philly"],
    },
    "PIT": {
        "abbr": "PIT",
        "short": "Steelers",
        "full": "Pittsburgh Steelers",
        "aliases": ["pittsburgh", "pittsburgh steelers", "steelers", "pit", "steelers"],
    },
    "SF": {
        "abbr": "SF",
        "short": "49ers",
        "full": "San Francisco 49ers",
        "aliases": [
            "san francisco", "san francisco 49ers", "49ers", "niners",
            "forty niners", "sf", "sfo",
        ],
    },
    "SEA": {
        "abbr": "SEA",
        "short": "Seahawks",
        "full": "Seattle Seahawks",
        "aliases": ["seattle", "seattle seahawks", "seahawks", "sea", "hawks"],
    },
    "TB": {
        "abbr": "TB",
        "short": "Buccaneers",
        "full": "Tampa Bay Buccaneers",
        "aliases": ["tampa", "tampa bay", "tampa bay buccaneers", "buccaneers", "bucs", "bucs", "tb", "tbb"],
    },
    "TEN": {
        "abbr": "TEN",
        "short": "Titans",
        "full": "Tennessee Titans",
        "aliases": ["tennessee", "tennessee titans", "titans", "ten"],
    },
    "WAS": {
        "abbr": "WAS",
        "short": "Commanders",
        "full": "Washington Commanders",
        "aliases": ["washington", "washington commanders", "commanders", "was", "wsh"],
    },
}

# Ambiguous city names that need a mascot.
AMBIGUOUS_TEAMS = {
    "new york": "Did you mean Giants or Jets?",
    "ny": "Did you mean Giants or Jets?",
    "los angeles": "Did you mean Rams or Chargers?",
    "la": "Did you mean Rams or Chargers?",
}


def _normalize(raw: str) -> str:
    text = (raw or "").strip().lower()
    text = text.replace(".", "").replace(",", "")
    text = " ".join(text.split())
    if text.startswith("the "):
        text = text[4:]
    return text


def build_alias_index() -> dict:
    index = {}
    for abbr, team in NFL_TEAMS.items():
        index[abbr.lower()] = abbr
        for alias in team["aliases"]:
            index[alias.lower()] = abbr
        index[team["short"].lower()] = abbr
        index[team["full"].lower()] = abbr
    return index


_ALIAS_INDEX = build_alias_index()


def resolve_team(raw: str) -> tuple[Optional[dict], Optional[str]]:
    """Resolve a user-typed team to a canonical NFL team.

    Returns (team_dict, error_message). On success error_message is None.
    On failure team_dict is None and error_message is a short SMS reply.
    """
    key = _normalize(raw)
    if not key:
        return None, "Reply YES + team (NFL), like YES Bengals."

    if key in AMBIGUOUS_TEAMS:
        return None, AMBIGUOUS_TEAMS[key]

    abbr = _ALIAS_INDEX.get(key)
    if not abbr:
        return None, "I didn't recognize that team. Reply YES + team (NFL), like YES Bengals."

    team = NFL_TEAMS[abbr]
    return {
        "abbr": team["abbr"],
        "short": team["short"],
        "full": team["full"],
    }, None
