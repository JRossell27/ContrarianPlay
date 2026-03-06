import argparse
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
import pandas as pd

ESPN_ODDS_URL = "https://www.espn.com/sports-betting/odds"
LEAGUE_ALLOWLIST = {
    "NBA",
    "NHL",
    "MLB",
    "NFL",
    "NCAAF",
    "NCAAM",
}  # NCAAM = men's college basketball on ESPN

# DraftKings league page links (static — always current)
DK_LEAGUE_LINKS: Dict[str, str] = {
    "NBA":   "https://sportsbook.draftkings.com/leagues/basketball/nba",
    "NFL":   "https://sportsbook.draftkings.com/leagues/football/nfl",
    "NHL":   "https://sportsbook.draftkings.com/leagues/hockey/nhl",
    "MLB":   "https://sportsbook.draftkings.com/leagues/baseball/mlb",
    "NCAAM": "https://sportsbook.draftkings.com/leagues/basketball/ncaab",
    "NCAAF": "https://sportsbook.draftkings.com/leagues/football/college-football",
}

# DraftKings internal league IDs (used for their public sportsbook API)
DK_LEAGUE_IDS: Dict[str, int] = {
    "NBA":   42648,
    "NFL":   88808,
    "NHL":   42133,
    "MLB":   84240,
    "NCAAM": 92483,
    "NCAAF": 87637,
}

# Action Network sport codes (for their free public bet % API)
AN_SPORT_MAP: Dict[str, str] = {
    "NBA":   "nba",
    "NFL":   "nfl",
    "NHL":   "nhl",
    "MLB":   "mlb",
    "NCAAM": "ncaab",
    "NCAAF": "ncaaf",
}

# Sport-specific defaults to avoid over/under-filtering moves.
SPORT_DEFAULTS = {
    "NBA": {"spread": 1.0, "total": 1.5, "ml_prob": 0.05},
    "NCAAM": {"spread": 1.0, "total": 1.5, "ml_prob": 0.05},
    "NFL": {"spread": 0.5, "total": 1.5, "ml_prob": 0.04},
    "NCAAF": {"spread": 0.5, "total": 1.5, "ml_prob": 0.04},
    "NHL": {"spread": 1.0, "total": 0.5, "ml_prob": 0.04},
    "MLB": {"spread": 1.0, "total": 1.0, "ml_prob": 0.04},
}

# Key numbers: moves crossing these thresholds are disproportionately significant
KEY_NUMBERS: Dict[str, frozenset] = {
    "NFL":   frozenset({3, 6, 7, 10, 14}),
    "NCAAF": frozenset({3, 6, 7, 10, 14}),
    "NBA":   frozenset({3, 5, 7}),
    "NCAAM": frozenset({3, 5, 7}),
}

# ─── Stadium coordinates for Open-Meteo weather lookups ──────────────────────
# None = retractable roof or enclosed dome — weather irrelevant for those games

NFL_STADIUM_COORDS: Dict[str, Optional[Tuple[float, float]]] = {
    "Arizona Cardinals":     None,      # State Farm Stadium — retractable roof
    "Atlanta Falcons":       None,      # Mercedes-Benz Stadium — dome
    "Baltimore Ravens":      (39.278, -76.623),
    "Buffalo Bills":         (42.774, -78.787),
    "Carolina Panthers":     (35.226, -80.853),
    "Chicago Bears":         (41.862, -87.617),
    "Cincinnati Bengals":    (39.095, -84.516),
    "Cleveland Browns":      (41.506, -81.700),
    "Dallas Cowboys":        None,      # AT&T Stadium — retractable roof
    "Denver Broncos":        (39.744, -105.020),
    "Detroit Lions":         None,      # Ford Field — dome
    "Green Bay Packers":     (44.501, -88.062),
    "Houston Texans":        None,      # NRG Stadium — retractable roof
    "Indianapolis Colts":    None,      # Lucas Oil Stadium — dome
    "Jacksonville Jaguars":  (30.324, -81.637),
    "Kansas City Chiefs":    (39.049, -94.484),
    "Las Vegas Raiders":     None,      # Allegiant Stadium — dome
    "Los Angeles Chargers":  None,      # SoFi Stadium — canopy roof
    "Los Angeles Rams":      None,      # SoFi Stadium — canopy roof
    "Miami Dolphins":        (25.958, -80.239),
    "Minnesota Vikings":     None,      # U.S. Bank Stadium — dome
    "New England Patriots":  (42.091, -71.264),
    "New Orleans Saints":    None,      # Caesars Superdome — dome
    "New York Giants":       (40.813, -74.074),
    "New York Jets":         (40.813, -74.074),
    "Philadelphia Eagles":   (39.901, -75.168),
    "Pittsburgh Steelers":   (40.447, -80.016),
    "San Francisco 49ers":   (37.403, -121.970),
    "Seattle Seahawks":      (47.595, -122.332),
    "Tampa Bay Buccaneers":  (27.976, -82.503),
    "Tennessee Titans":      (36.167, -86.771),
    "Washington Commanders": (38.908, -76.865),
}

MLB_STADIUM_COORDS: Dict[str, Optional[Tuple[float, float]]] = {
    "Arizona Diamondbacks":  None,      # Chase Field — retractable roof
    "Atlanta Braves":        (33.891, -84.468),
    "Baltimore Orioles":     (39.284, -76.622),
    "Boston Red Sox":        (42.347, -71.097),
    "Chicago Cubs":          (41.948, -87.655),
    "Chicago White Sox":     (41.830, -87.634),
    "Cincinnati Reds":       (39.097, -84.508),
    "Cleveland Guardians":   (41.496, -81.685),
    "Colorado Rockies":      (39.756, -104.994),
    "Detroit Tigers":        (42.339, -83.049),
    "Houston Astros":        None,      # Minute Maid Park — retractable roof
    "Kansas City Royals":    (39.052, -94.480),
    "Los Angeles Angels":    (33.800, -117.883),
    "Los Angeles Dodgers":   (34.074, -118.240),
    "Miami Marlins":         None,      # loanDepot park — retractable roof
    "Milwaukee Brewers":     (43.028, -87.971),
    "Minnesota Twins":       (44.982, -93.278),
    "New York Mets":         (40.757, -73.846),
    "New York Yankees":      (40.830, -73.926),
    "Oakland Athletics":     (37.752, -122.201),
    "Philadelphia Phillies": (39.906, -75.167),
    "Pittsburgh Pirates":    (40.447, -80.006),
    "San Diego Padres":      (32.708, -117.157),
    "San Francisco Giants":  (37.779, -122.389),
    "Seattle Mariners":      None,      # T-Mobile Park — retractable roof
    "St. Louis Cardinals":   (38.623, -90.193),
    "Tampa Bay Rays":        None,      # Tropicana Field — dome
    "Texas Rangers":         None,      # Globe Life Field — retractable roof
    "Toronto Blue Jays":     None,      # Rogers Centre — dome
    "Washington Nationals":  (38.873, -77.007),
}

# WMO weather codes indicating precipitation
_PRECIP_CODES = frozenset({
    51, 53, 55,       # Drizzle
    61, 63, 65,       # Rain
    71, 73, 75, 77,   # Snow / snow grains
    80, 81, 82,       # Rain showers
    85, 86,           # Snow showers
    95, 96, 99,       # Thunderstorm
})
_SNOW_CODES = frozenset({71, 73, 75, 77, 85, 86})

# Covers.com sport codes
COVERS_SPORT_MAP: Dict[str, str] = {
    "NFL":   "nfl",
    "NBA":   "nba",
    "MLB":   "mlb",
    "NCAAF": "ncaaf",
    "NCAAM": "ncaab",
    "NHL":   "nhl",
}

# ESPN sport/league path pairs for schedule + scoreboard lookups
ESPN_SPORT_PATHS: Dict[str, Tuple[str, str]] = {
    "NFL":   ("football",    "nfl"),
    "NBA":   ("basketball",  "nba"),
    "NHL":   ("hockey",      "nhl"),
    "MLB":   ("baseball",    "mlb"),
    "NCAAF": ("football",    "college-football"),
    "NCAAM": ("basketball",  "mens-college-basketball"),
}

# Browser-like headers for scraping
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_odds_page() -> str:
    resp = requests.get(ESPN_ODDS_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_state(html: str) -> Dict:
    match = re.search(r"window\['__espnfitt__'\]=(\{.*?\});", html, re.S)
    if not match:
        raise RuntimeError("Could not find ESPN odds state blob.")
    return json.loads(match.group(1))


def american_to_prob(odds_str: Optional[str]) -> Optional[float]:
    if not odds_str:
        return None
    try:
        price = float(odds_str)
    except (TypeError, ValueError):
        return None
    if price > 0:
        return 100 / (price + 100)
    return -price / (-price + 100)


def parse_point(line: Optional[str]) -> Optional[float]:
    if line is None:
        return None
    cleaned = line.lower()
    if cleaned.startswith(("o", "u")):
        cleaned = cleaned[1:]
    try:
        return float(cleaned)
    except ValueError:
        return None


def iso_to_local_text(iso_str: str) -> str:
    """Convert ISO timestamp to a readable ET string with Today/Tomorrow prefix."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        # Convert to US Eastern time (UTC-5 standard, UTC-4 daylight — approximate)
        et_offset = timedelta(hours=-5)
        dt_et = dt + et_offset
        now_et = datetime.now(timezone.utc) + et_offset

        if dt_et.date() == now_et.date():
            day_label = "Today"
        elif dt_et.date() == (now_et + timedelta(days=1)).date():
            day_label = "Tomorrow"
        else:
            day_label = dt_et.strftime("%a %b %-d")

        return f"{day_label} {dt_et.strftime('%-I:%M %p')} ET"
    except Exception:
        return iso_str


def _normalize_team(name: str) -> str:
    return name.lower().replace(".", "").replace("'", "").strip()


def _score(delta: float, threshold: float) -> float:
    """Normalize move magnitude to [0.0, 1.0]. A move of 2× threshold scores 1.0."""
    return min(abs(delta) / (2.0 * threshold), 1.0)


def confidence_label(score: float) -> str:
    """Human-readable signal-strength tier for a pick score.

    These tiers describe how many independent signals agree and how large the
    move was — NOT a probability of winning the bet.
    """
    if score >= 0.75:
        return "LOCK"
    if score >= 0.50:
        return "STRONG"
    if score >= 0.25:
        return "LEAN"
    return "WATCH"


def _crosses_key_number(open_val: float, cur_val: float, league: str) -> bool:
    keys = KEY_NUMBERS.get(league, frozenset())
    if not keys:
        return False
    lo = min(abs(open_val), abs(cur_val))
    hi = max(abs(open_val), abs(cur_val))
    return any(lo < k <= hi for k in keys)


def _pick_team(text: str, home_team: str, away_team: str) -> Optional[str]:
    """Return which team a pick is recommending, or None for totals picks."""
    for team in (home_team, away_team):
        if text.startswith(f"FOLLOW: {team}"):
            return team
    return None


def iter_events(state: Dict) -> Iterable[Tuple[str, Dict]]:
    odds_block = state.get("page", {}).get("content", {}).get("odds", {})
    for league_block in odds_block.get("odds", []):
        league = league_block.get("displayValue")
        if league not in LEAGUE_ALLOWLIST:
            continue
        for line in league_block.get("lines", []):
            yield league, line


# ─── Free external data sources ──────────────────────────────────────────────

def fetch_action_network(sport: str, date_str: Optional[str] = None) -> List[Dict]:
    """Fetch public bet % data from Action Network (free — no API key needed).

    sport:    ESPN league code (NBA, NFL, etc.)
    date_str: YYYYMMDD string; defaults to today.

    Returns list of game dicts, each potentially containing bet % fields.
    Returns [] silently if the request fails or the API is unavailable.
    """
    an_sport = AN_SPORT_MAP.get(sport)
    if not an_sport:
        return []
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")
    url = f"https://api.actionnetwork.com/web/v1/games?sport={an_sport}&date={date_str}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        return resp.json().get("games", [])
    except Exception:
        return []


# ─── Open-Meteo weather (completely free, no API key) ─────────────────────────

def get_stadium_coords(
    home_team: str, league: str
) -> Optional[Tuple[float, float]]:
    """Return outdoor stadium (lat, lon) for home team, or None if indoor/dome."""
    if league == "NFL":
        return NFL_STADIUM_COORDS.get(home_team)
    if league == "MLB":
        return MLB_STADIUM_COORDS.get(home_team)
    # NCAAF: nearly all outdoor, but we lack a full college stadium DB — skip
    return None


def parse_game_dt(iso_str: str) -> Optional[datetime]:
    """Parse an ESPN ISO timestamp to a UTC-aware datetime."""
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except Exception:
        return None


def fetch_weather(
    lat: float, lon: float, game_dt: Optional[datetime] = None
) -> Optional[Dict]:
    """Fetch weather forecast from Open-Meteo.

    Open-Meteo is 100% free with no API key: https://open-meteo.com/
    10,000 calls/day limit, 7-day hourly forecasts.

    Returns a dict with temp_f, wind_mph, wind_dir, precipitation_in,
    weather_code — all at the game's scheduled hour. Returns None on failure.
    """
    params = {
        "latitude":          round(lat, 4),
        "longitude":         round(lon, 4),
        "current_weather":   "true",
        "hourly":            "temperature_2m,windspeed_10m,winddirection_10m,precipitation,weathercode",
        "temperature_unit":  "fahrenheit",
        "windspeed_unit":    "mph",
        "precipitation_unit": "inch",
        "forecast_days":     7,
        "timezone":          "auto",
    }
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params=params,
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    hourly = data.get("hourly", {})
    times  = hourly.get("time", [])

    def _at(field: str, idx: int):
        vals = hourly.get(field, [])
        return vals[idx] if 0 <= idx < len(vals) else None

    if game_dt and times:
        game_utc = game_dt.astimezone(timezone.utc)
        best_idx, best_diff = 0, float("inf")
        for i, t_str in enumerate(times):
            try:
                t = datetime.fromisoformat(t_str)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                diff = abs((t.astimezone(timezone.utc) - game_utc).total_seconds())
                if diff < best_diff:
                    best_diff, best_idx = diff, i
            except Exception:
                continue
        idx = best_idx
    elif times:
        idx = 0
    else:
        cw = data.get("current_weather", {})
        return {
            "temp_f":          cw.get("temperature"),
            "wind_mph":        cw.get("windspeed"),
            "wind_dir":        cw.get("winddirection"),
            "precipitation_in": None,
            "weather_code":    cw.get("weathercode"),
        }

    return {
        "temp_f":          _at("temperature_2m",    idx),
        "wind_mph":        _at("windspeed_10m",     idx),
        "wind_dir":        _at("winddirection_10m", idx),
        "precipitation_in": _at("precipitation",   idx),
        "weather_code":    _at("weathercode",       idx),
    }


def evaluate_weather_impact(weather: Optional[Dict]) -> Optional[str]:
    """Assess weather conditions and return a betting-relevant description.

    Research-backed thresholds for NFL totals:
      Wind ≥ 15 mph  → −0.5 to −1.0 pts on average total
      Wind ≥ 20 mph  → −1.5 to −2.5 pts
      Wind ≥ 25 mph  → −3+ pts (historically game-changing)
      Rain/snow      → additional −1 to −2 pts
      Temp ≤ 25 °F   → additional −1 to −1.5 pts

    Returns a short string for display, or None if conditions are neutral.
    """
    if not weather:
        return None

    wind  = weather.get("wind_mph") or 0.0
    temp  = weather.get("temp_f")
    precip = weather.get("precipitation_in") or 0.0
    code  = weather.get("weather_code") or 0

    factors: List[str] = []
    score = 0  # higher = stronger under lean

    if wind >= 25:
        factors.append(f"💨 {wind:.0f} mph wind")
        score += 3
    elif wind >= 20:
        factors.append(f"💨 {wind:.0f} mph wind")
        score += 2
    elif wind >= 15:
        factors.append(f"💨 {wind:.0f} mph wind")
        score += 1

    is_snow = code in _SNOW_CODES
    has_precip = precip >= 0.05 or code in _PRECIP_CODES
    if is_snow:
        factors.append("❄️ snow")
        score += 3
    elif has_precip:
        factors.append("🌧️ rain")
        score += 2

    if temp is not None:
        if temp <= 10:
            factors.append(f"🥶 {temp:.0f}°F")
            score += 3
        elif temp <= 25:
            factors.append(f"❄️ {temp:.0f}°F")
            score += 2
        elif temp <= 35:
            factors.append(f"🌡️ {temp:.0f}°F")
            score += 1

    if not factors:
        return None   # clear, mild, no impact

    if score >= 5:
        lean = "STRONG under lean"
    elif score >= 3:
        lean = "moderate under lean"
    elif score >= 1:
        lean = "mild under lean"
    else:
        return None

    return f"🌦️ Weather: {', '.join(factors)} → {lean}"


# ─── Covers.com public betting consensus (free, no API key) ────────────────────

def fetch_covers_consensus(league: str) -> List[Dict]:
    """Scrape public betting percentages from Covers.com consensus page.

    URL pattern: https://contests.covers.com/consensus/topconsensus/{sport}/overall
    Free HTML page — no account or API key required.

    Returns list of dicts with keys:
      home_team, away_team, home_pct (int 0-100), away_pct,
      home_money_pct, away_money_pct  — any may be None.
    Returns [] silently on any failure.
    """
    sport = COVERS_SPORT_MAP.get(league)
    if not sport:
        return []

    url = f"https://contests.covers.com/consensus/topconsensus/{sport}/overall"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        html = resp.text
    except Exception:
        return []

    # Strategy 1: look for embedded JSON in <script> tags (React/Next.js SPAs)
    for pattern in (
        r"window\.__NEXT_DATA__\s*=\s*(\{.*?\})\s*;",
        r"window\.__data\s*=\s*(\{.*?\})\s*;",
        r"window\.consensus_data\s*=\s*(\{.*?\})\s*;",
    ):
        m = re.search(pattern, html, re.DOTALL)
        if m:
            try:
                result = _parse_covers_json(json.loads(m.group(1)))
                if result:
                    return result
            except Exception:
                pass

    # Strategy 2: parse HTML tables/divs
    return _parse_covers_html(html)


def _pct_int(val) -> Optional[int]:
    """Convert '65%', 0.65, or 65 → int 65. Returns None on failure."""
    if val is None:
        return None
    try:
        s = str(val).replace("%", "").strip()
        f = float(s)
        if 0.0 < f <= 1.0:
            f *= 100
        return int(round(f))
    except (ValueError, TypeError):
        return None


def _parse_covers_json(data: Dict) -> List[Dict]:
    """Try to extract game consensus data from Covers.com embedded JSON."""
    games = []
    for container_key in ("games", "matchups", "consensus", "events", "picks", "data"):
        items = data.get(container_key) or []
        if not isinstance(items, list):
            continue
        for item in items:
            try:
                home = item.get("homeTeam") or item.get("home_team") or {}
                away = item.get("awayTeam") or item.get("away_team") or {}
                home_name = (home.get("fullName") or home.get("displayName")
                             or home.get("name") or "")
                away_name = (away.get("fullName") or away.get("displayName")
                             or away.get("name") or "")
                if not home_name or not away_name:
                    continue
                con = item.get("consensus") or item.get("spreadConsensus") or {}
                games.append({
                    "home_team":      home_name,
                    "away_team":      away_name,
                    "home_pct":       _pct_int(con.get("homePct")    or home.get("spreadPct") or home.get("ticketPct")),
                    "away_pct":       _pct_int(con.get("awayPct")    or away.get("spreadPct") or away.get("ticketPct")),
                    "home_money_pct": _pct_int(con.get("homeMoneyPct") or home.get("moneyPct")),
                    "away_money_pct": _pct_int(con.get("awayMoneyPct") or away.get("moneyPct")),
                })
            except Exception:
                continue
    return games


def _parse_covers_html(html: str) -> List[Dict]:
    """Parse Covers consensus HTML tables/divs with BeautifulSoup."""
    soup = BeautifulSoup(html, "html.parser")
    games: List[Dict] = []

    # Strategy A: HTML tables — Covers renders game rows in tables on some layouts
    for table in soup.find_all("table"):
        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            pcts  = [c for c in cells if re.match(r"^\d{1,3}%$", c)]
            if len(pcts) < 2:
                continue
            teams = [c for c in cells[:4]
                     if c and len(c) > 2 and not re.match(r"^[\d%+\-./: ]+$", c)]
            if len(teams) < 2:
                continue
            games.append({
                "away_team":      teams[0],
                "home_team":      teams[1],
                "away_pct":       _pct_int(pcts[0]),
                "home_pct":       _pct_int(pcts[1]),
                "away_money_pct": _pct_int(pcts[2]) if len(pcts) > 2 else None,
                "home_money_pct": _pct_int(pcts[3]) if len(pcts) > 3 else None,
            })
    if games:
        return games

    # Strategy B: div-based layout (React components often render % in spans)
    for div in soup.find_all("div", class_=re.compile(
        r"matchup|consensus|pick|game-row|game-item|event-row", re.I
    )):
        texts = [s.strip() for s in div.strings if s.strip()]
        pcts  = [_pct_int(t) for t in texts if re.match(r"^\d{1,3}%$", t)]
        teams = [t for t in texts
                 if t and len(t) > 2 and not re.match(r"^[\d%+\-./: ]+$", t)]
        if len(pcts) >= 2 and len(teams) >= 2:
            games.append({
                "away_team":      teams[0],
                "home_team":      teams[1],
                "away_pct":       pcts[0],
                "home_pct":       pcts[1],
                "away_money_pct": pcts[2] if len(pcts) > 2 else None,
                "home_money_pct": pcts[3] if len(pcts) > 3 else None,
            })
    return games


def match_covers_game(
    home_team: str, away_team: str, covers_games: List[Dict]
) -> Optional[Dict]:
    """Match an ESPN game to a Covers consensus entry by fuzzy team name."""
    home_norm = _normalize_team(home_team)
    away_norm = _normalize_team(away_team)
    for g in covers_games:
        c_home = _normalize_team(g.get("home_team", ""))
        c_away = _normalize_team(g.get("away_team", ""))
        home_ok = (home_norm in c_home or c_home in home_norm
                   or (home_norm.split() and home_norm.split()[-1] in c_home))
        away_ok = (away_norm in c_away or c_away in away_norm
                   or (away_norm.split() and away_norm.split()[-1] in c_away))
        if home_ok and away_ok:
            return g
    return None


# ─── ESPN schedule API for rest-day context (free, no auth) ───────────────────

def fetch_recent_games(league: str, days_back: int = 8) -> Dict[str, datetime]:
    """Scan ESPN scoreboard for the past N days to find each team's last game date.

    Uses: https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard
    Free, no authentication required.

    Returns: normalized_team_name → last_game_datetime (UTC-aware).
    """
    path = ESPN_SPORT_PATHS.get(league)
    if not path:
        return {}
    sport, league_code = path
    team_last: Dict[str, datetime] = {}

    for delta in range(1, days_back + 1):
        date_str = (datetime.now(timezone.utc) - timedelta(days=delta)).strftime("%Y%m%d")
        url = (
            f"https://site.api.espn.com/apis/site/v2/sports"
            f"/{sport}/{league_code}/scoreboard?dates={date_str}&limit=100"
        )
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=8)
            if not resp.ok:
                continue
            data = resp.json()
        except Exception:
            continue

        for event in data.get("events", []):
            try:
                gdt = datetime.fromisoformat(
                    event.get("date", "").replace("Z", "+00:00")
                )
            except Exception:
                continue
            comps = (event.get("competitions") or [{}])[0]
            for ct in comps.get("competitors", []):
                t_info = ct.get("team", {})
                name = _normalize_team(
                    t_info.get("displayName") or t_info.get("name") or ""
                )
                if name and (name not in team_last or gdt > team_last[name]):
                    team_last[name] = gdt

    return team_last


def compute_rest_days(
    team_name: str,
    last_games: Dict[str, datetime],
    game_dt: Optional[datetime],
) -> Optional[int]:
    """Return integer days of rest before the current game, or None if unknown."""
    if not game_dt or not last_games:
        return None
    last = last_games.get(_normalize_team(team_name))
    if last is None:
        return None
    delta = game_dt.astimezone(timezone.utc) - last.astimezone(timezone.utc)
    return max(0, delta.days)


def rest_day_note(days: Optional[int], league: str) -> Optional[str]:
    """Return a betting-relevant note about rest days, or None if unremarkable."""
    if days is None:
        return None
    if league in ("NBA", "NCAAM"):
        if days <= 1:
            return f"😴 Back-to-back ({days}d rest) — teams cover spread less often on B2B"
        if days >= 5:
            return f"✨ {days} days rest — well-rested, small performance edge"
    elif league in ("NFL", "NCAAF"):
        if days <= 4:
            return f"⚡ Short week ({days}d rest) — injury risk up, performance historically dips"
        if days >= 13:
            return f"✨ Coming off bye ({days}d rest) — bye-week prep edge historically ~+1.5 pts"
    elif league == "MLB":
        if days == 0:
            return "⚠️ Double-header or unusual scheduling"
    return None


def match_an_game(
    home_team: str, away_team: str, an_games: List[Dict]
) -> Optional[Dict]:
    """Try to match an ESPN game to an Action Network game by fuzzy team name."""
    home_norm = _normalize_team(home_team)
    away_norm = _normalize_team(away_team)
    for game in an_games:
        ht = game.get("home_team") or {}
        at = game.get("away_team") or {}
        an_home = _normalize_team(
            ht.get("full_name") or ht.get("name") or ht.get("abbr") or ""
        )
        an_away = _normalize_team(
            at.get("full_name") or at.get("name") or at.get("abbr") or ""
        )
        home_match = home_norm in an_home or an_home in home_norm or (
            len(home_norm) >= 4 and home_norm[-4:] in an_home
        )
        away_match = away_norm in an_away or an_away in away_norm or (
            len(away_norm) >= 4 and away_norm[-4:] in an_away
        )
        if home_match and away_match:
            return game
    return None


def get_bet_pcts(an_game: Optional[Dict]) -> Dict[str, Optional[int]]:
    """Extract public betting percentages from an Action Network game object.

    Returns a dict with these optional integer keys (0-100):
      spread_home_pct   — % of spread tickets on home team
      spread_away_pct   — % of spread tickets on away team
      money_home_pct    — % of spread money on home team
      money_away_pct    — % of spread money on away team
      ml_home_pct       — % of ML tickets on home team
      ml_away_pct       — % of ML tickets on away team
      over_pct          — % of total tickets on Over
      under_pct         — % of total tickets on Under
    """
    result: Dict[str, Optional[int]] = {}
    if not an_game:
        return result

    teams = an_game.get("teams") or []
    home_t = next((t for t in teams if t.get("is_home") or t.get("homeAway") == "home"), None)
    away_t = next((t for t in teams if not (t.get("is_home") or t.get("homeAway") == "home")), None)

    def _pct(obj: Optional[Dict], *keys: str) -> Optional[int]:
        if not obj:
            return None
        for k in keys:
            v = obj.get(k)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
        return None

    result["spread_home_pct"] = _pct(home_t, "spread_tickets", "spread_pct", "tickets_pct")
    result["spread_away_pct"] = _pct(away_t, "spread_tickets", "spread_pct", "tickets_pct")
    result["money_home_pct"]  = _pct(home_t, "spread_money", "money_pct")
    result["money_away_pct"]  = _pct(away_t, "spread_money", "money_pct")
    result["ml_home_pct"]     = _pct(home_t, "ml_tickets", "ml_pct")
    result["ml_away_pct"]     = _pct(away_t, "ml_tickets", "ml_pct")
    result["over_pct"]        = _pct(an_game, "over_tickets", "over_pct")
    result["under_pct"]       = _pct(an_game, "under_tickets", "under_pct")
    return result


def rlm_signal(
    pick_text: str,
    home_team: str,
    away_team: str,
    bet_pcts: Dict[str, Optional[int]],
) -> Optional[str]:
    """Detect Reverse Line Movement (RLM): the line moved AGAINST the public.

    RLM means sharp (professional) money is on the side the public is fading.
    It's one of the strongest indicators that smart money disagrees with the crowd.

    Returns a descriptive string if RLM is detected, else None.
    """
    if not bet_pcts:
        return None

    clean = pick_text.replace("[MULTI-SIGNAL] ", "")
    is_total = clean.startswith("FOLLOW: Over") or clean.startswith("FOLLOW: Under")

    if is_total:
        over_pct = bet_pcts.get("over_pct")
        under_pct = bet_pcts.get("under_pct")
        if over_pct is None and under_pct is None:
            return None
        if clean.startswith("FOLLOW: Over") and over_pct is not None and over_pct < 45:
            return (
                f"📡 RLM: Only {over_pct}% of public bets on Over — "
                f"line still moved Up. Sharp money may be on Over despite public fading it."
            )
        if clean.startswith("FOLLOW: Under") and under_pct is not None and under_pct < 45:
            return (
                f"📡 RLM: Only {under_pct}% of public bets on Under — "
                f"line still moved Down. Sharp money may be on Under despite public fading it."
            )
        return None

    followed = _pick_team(clean, home_team, away_team)
    if followed is None:
        return None

    is_home = followed == home_team
    spread_pct = bet_pcts.get("spread_home_pct" if is_home else "spread_away_pct")
    money_pct  = bet_pcts.get("money_home_pct" if is_home else "money_away_pct")

    if spread_pct is None:
        return None

    if spread_pct < 40:
        money_note = f", {money_pct}% of money" if money_pct is not None else ""
        return (
            f"📡 RLM: Only {spread_pct}% of public bets on {followed}{money_note} — "
            f"but the line still moved their way. Classic sharp-money signal."
        )
    if spread_pct > 60:
        money_note = f", {money_pct}% of money" if money_pct is not None else ""
        return (
            f"⚠️ Public side: {spread_pct}% of bets on {followed}{money_note}. "
            f"Line moved with the public — less convincing as a sharp signal."
        )
    return None


def fetch_dk_odds(league: str) -> List[Dict]:
    """Try to fetch current DraftKings odds via their public API (no auth).

    DraftKings does not officially expose a public API, but they have
    several endpoints that work without authentication. Returns empty list
    if the API is unavailable or the response format is unexpected.
    """
    league_id = DK_LEAGUE_IDS.get(league)
    if not league_id:
        return []

    # Try DraftKings' known public sportsbook API endpoints
    urls = [
        f"https://sportsbook.draftkings.com/api/odds/v1/leagues/{league_id}/categories/743/subcategories/5517",
        f"https://api.draftkings.com/lineups/v1/tiers/offering/sports/{league_id}/draftgroups?format=json",
    ]
    for url in urls:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=8)
            if resp.ok:
                data = resp.json()
                events = (
                    data.get("eventGroups")
                    or data.get("events")
                    or data.get("offers")
                    or []
                )
                if isinstance(events, list) and events:
                    return events
        except Exception:
            continue
    return []


def match_dk_game(
    home_team: str, away_team: str, dk_events: List[Dict]
) -> Optional[Dict]:
    """Try to match an ESPN game to a DraftKings event by team name."""
    home_norm = _normalize_team(home_team)
    away_norm = _normalize_team(away_team)
    for ev in dk_events:
        name = _normalize_team(ev.get("name") or ev.get("eventName") or "")
        if (home_norm in name or any(w in name for w in home_norm.split())) and \
           (away_norm in name or any(w in name for w in away_norm.split())):
            return ev
    return None


# ─── Core signal detection ─────────────────────────────────────────────────

def collect_market_moves(
    league: str,
    line: Dict,
    spread_threshold: float,
    total_threshold: float,
    moneyline_threshold: float,
) -> List[Tuple[str, float]]:
    """Return list of (pick_text, signal_score) tuples sorted by score desc.

    Signal score components (additive, capped at 1.0):
      - Base:              normalized move magnitude (2× threshold → 1.0 base).
      - Convergence bonus (+0.25): spread and ML both moved toward the same team.
      - Vig confirmation  (+0.15): juice tightened on the steamed side.
      - Key number bonus  (+0.20): spread move crossed a key landing number.
      - Multi-signal bonus(+0.20): spread AND ML both independently triggered.
      - Conflict penalty  (-0.20): spread and ML moved in opposite directions.

    Note: scores describe signal strength, not win probability.
    """
    picks: List[Tuple[str, float]] = []

    competitors = line.get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        return picks
    home_team = home.get("team", {}).get("displayName", "Home")
    away_team = away.get("team", {}).get("displayName", "Away")

    odds_list = line.get("odds", [])
    if not odds_list:
        return picks
    odds = odds_list[0]

    # ── Moneyline ────────────────────────────────────────────────────────────
    moneyline = odds.get("moneyline")
    home_ml_delta: Optional[float] = None
    ml_home_triggered = False
    ml_away_triggered = False
    if moneyline:
        home_ml_open = american_to_prob(moneyline.get("home", {}).get("open", {}).get("odds"))
        home_ml_cur = american_to_prob(moneyline.get("home", {}).get("close", {}).get("odds"))
        away_ml_open = american_to_prob(moneyline.get("away", {}).get("open", {}).get("odds"))
        away_ml_cur = american_to_prob(moneyline.get("away", {}).get("close", {}).get("odds"))
        if None not in (home_ml_open, home_ml_cur, away_ml_open, away_ml_cur):
            home_ml_delta = home_ml_cur - home_ml_open
            ml_home_triggered = home_ml_delta >= moneyline_threshold
            ml_away_triggered = home_ml_delta <= -moneyline_threshold

    # ── Spreads ──────────────────────────────────────────────────────────────
    spread = odds.get("pointSpread")
    if spread:
        home_open = parse_point(spread.get("home", {}).get("open", {}).get("line"))
        home_cur = parse_point(spread.get("home", {}).get("close", {}).get("line"))
        away_open = parse_point(spread.get("away", {}).get("open", {}).get("line"))
        away_cur = parse_point(spread.get("away", {}).get("close", {}).get("line"))
        home_open_price = american_to_prob(spread.get("home", {}).get("open", {}).get("odds"))
        home_cur_price = american_to_prob(spread.get("home", {}).get("close", {}).get("odds"))
        away_open_price = american_to_prob(spread.get("away", {}).get("open", {}).get("odds"))
        away_cur_price = american_to_prob(spread.get("away", {}).get("close", {}).get("odds"))
        if None not in (home_open, home_cur, away_open, away_cur):
            if league in {"NHL", "MLB"} and abs(home_open) == 1.5 and abs(home_cur) == 1.5:
                if home_open_price is not None and home_cur_price is not None:
                    price_delta = home_cur_price - home_open_price
                    if abs(price_delta) >= moneyline_threshold:
                        base = _score(price_delta, moneyline_threshold)
                        converge = (
                            home_ml_delta is not None
                            and (
                                (price_delta > 0 and home_ml_delta > 0)
                                or (price_delta < 0 and home_ml_delta < 0)
                            )
                        )
                        conflict = (
                            home_ml_delta is not None
                            and (
                                (price_delta > 0 and home_ml_delta < 0)
                                or (price_delta < 0 and home_ml_delta > 0)
                            )
                        )
                        score = max(
                            min(base + (0.25 if converge else 0.0), 1.0)
                            - (0.20 if conflict else 0.0),
                            0.0,
                        )
                        if price_delta > 0:
                            picks.append((
                                f"FOLLOW: {home_team} {home_cur:+} — price firmed by {price_delta*100:+.1f}pp at same line",
                                score,
                            ))
                        else:
                            picks.append((
                                f"FOLLOW: {away_team} {away_cur:+} — price firmed by {-price_delta*100:+.1f}pp at same line",
                                score,
                            ))
            else:
                delta_line = home_cur - home_open
                sign_flipped = (home_open != 0 and home_cur != 0
                                and (home_open < 0) != (home_cur < 0))
                if sign_flipped:
                    delta = abs(delta_line)
                else:
                    delta = abs(home_cur) - abs(home_open)

                price_delta: Optional[float] = None
                if home_open_price is not None and home_cur_price is not None:
                    price_delta = home_cur_price - home_open_price
                away_price_delta: Optional[float] = None
                if away_open_price is not None and away_cur_price is not None:
                    away_price_delta = away_cur_price - away_open_price

                effective_delta = delta_line if sign_flipped else (abs(home_cur) - abs(home_open))

                if effective_delta <= -spread_threshold:
                    base = _score(effective_delta, spread_threshold)
                    converge = home_ml_delta is not None and home_ml_delta > 0
                    vig_confirm = away_price_delta is not None and away_price_delta > 0
                    key_num = _crosses_key_number(home_open, home_cur, league)
                    multi = ml_home_triggered
                    conflict = home_ml_delta is not None and home_ml_delta < -moneyline_threshold
                    score = max(
                        min(
                            base
                            + (0.25 if converge else 0.0)
                            + (0.15 if vig_confirm else 0.0)
                            + (0.20 if key_num else 0.0)
                            + (0.20 if multi else 0.0),
                            1.0,
                        ) - (0.20 if conflict else 0.0),
                        0.0,
                    )
                    label = "[MULTI-SIGNAL] " if multi else ""
                    picks.append((
                        f"{label}FOLLOW: {home_team} {home_cur:+} (was {home_open:+}) — line shift {delta_line:+.1f}",
                        score,
                    ))
                elif effective_delta >= spread_threshold:
                    base = _score(effective_delta, spread_threshold)
                    converge = home_ml_delta is not None and home_ml_delta < 0
                    vig_confirm = price_delta is not None and price_delta > 0
                    key_num = _crosses_key_number(home_open, home_cur, league)
                    multi = ml_away_triggered
                    conflict = home_ml_delta is not None and home_ml_delta > moneyline_threshold
                    score = max(
                        min(
                            base
                            + (0.25 if converge else 0.0)
                            + (0.15 if vig_confirm else 0.0)
                            + (0.20 if key_num else 0.0)
                            + (0.20 if multi else 0.0),
                            1.0,
                        ) - (0.20 if conflict else 0.0),
                        0.0,
                    )
                    label = "[MULTI-SIGNAL] " if multi else ""
                    picks.append((
                        f"{label}FOLLOW: {away_team} {away_cur:+} (was {away_open:+}) — line shift {delta_line:+.1f}",
                        score,
                    ))
                elif (
                    price_delta is not None
                    and abs(price_delta) >= moneyline_threshold
                    and home_cur == home_open
                ):
                    base = _score(price_delta, moneyline_threshold)
                    converge = (
                        home_ml_delta is not None
                        and (
                            (price_delta > 0 and home_ml_delta > 0)
                            or (price_delta < 0 and home_ml_delta < 0)
                        )
                    )
                    conflict = (
                        home_ml_delta is not None
                        and (
                            (price_delta > 0 and home_ml_delta < -moneyline_threshold)
                            or (price_delta < 0 and home_ml_delta > moneyline_threshold)
                        )
                    )
                    score = max(
                        min(base + (0.25 if converge else 0.0), 1.0)
                        - (0.20 if conflict else 0.0),
                        0.0,
                    )
                    if price_delta > 0:
                        picks.append((
                            f"FOLLOW: {home_team} {home_cur:+} — price firmed by {price_delta*100:+.1f}pp at same line",
                            score,
                        ))
                    else:
                        picks.append((
                            f"FOLLOW: {away_team} {away_cur:+} — price firmed by {-price_delta*100:+.1f}pp at same line",
                            score,
                        ))

    # ── Totals ───────────────────────────────────────────────────────────────
    total = odds.get("total")
    if total:
        open_point = parse_point(total.get("over", {}).get("open", {}).get("line"))
        cur_point = parse_point(total.get("over", {}).get("close", {}).get("line"))
        over_open_price = american_to_prob(total.get("over", {}).get("open", {}).get("odds"))
        over_cur_price = american_to_prob(total.get("over", {}).get("close", {}).get("odds"))
        under_open_price = american_to_prob(total.get("under", {}).get("open", {}).get("odds"))
        under_cur_price = american_to_prob(total.get("under", {}).get("close", {}).get("odds"))
        if None not in (open_point, cur_point):
            delta = cur_point - open_point
            if delta >= total_threshold:
                base = _score(delta, total_threshold)
                vig_confirm = (
                    over_open_price is not None
                    and over_cur_price is not None
                    and over_cur_price > over_open_price
                )
                score = min(base + (0.15 if vig_confirm else 0.0), 1.0)
                picks.append((
                    f"FOLLOW: Over {cur_point:.1f} — total moved {delta:+.1f} from {open_point:.1f}",
                    score,
                ))
            elif delta <= -total_threshold:
                base = _score(delta, total_threshold)
                vig_confirm = (
                    under_open_price is not None
                    and under_cur_price is not None
                    and under_cur_price > under_open_price
                )
                score = min(base + (0.15 if vig_confirm else 0.0), 1.0)
                picks.append((
                    f"FOLLOW: Under {cur_point:.1f} — total moved {delta:+.1f} from {open_point:.1f}",
                    score,
                ))

    # ── Moneylines ───────────────────────────────────────────────────────────
    if moneyline and home_ml_delta is not None:
        if home_ml_delta >= moneyline_threshold:
            swing = home_ml_delta * 100
            score = _score(home_ml_delta, moneyline_threshold)
            picks.append((
                f"FOLLOW: {home_team} ML {moneyline.get('home', {}).get('close', {}).get('odds')} "
                f"— market boosted home by {swing:+.1f}pp",
                score,
            ))
        elif home_ml_delta <= -moneyline_threshold:
            swing = -home_ml_delta * 100
            score = _score(home_ml_delta, moneyline_threshold)
            picks.append((
                f"FOLLOW: {away_team} ML {moneyline.get('away', {}).get('close', {}).get('odds')} "
                f"— market boosted away by {swing:+.1f}pp",
                score,
            ))

    picks.sort(key=lambda t: t[1], reverse=True)
    return picks


# ─── Injury helpers ───────────────────────────────────────────────────────────

def fetch_injuries(league: str) -> Dict[str, List[Dict[str, str]]]:
    urls = {
        "NBA": "https://www.espn.com/nba/injuries",
        "NFL": "https://www.espn.com/nfl/injuries",
        "NHL": "https://www.espn.com/nhl/injuries",
        "MLB": "https://www.espn.com/mlb/injuries",
        "NCAAF": "https://www.espn.com/college-football/injuries",
        "NCAAM": "https://www.espn.com/mens-college-basketball/injuries",
    }
    url = urls.get(league)
    if not url:
        return {}
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception:
        return {}
    soup = BeautifulSoup(resp.text, "html.parser")
    result: Dict[str, List[Dict[str, str]]] = {}
    for section in soup.find_all("section"):
        header = section.find("h2")
        table = section.find("table")
        if not header or not table:
            continue
        team_name = header.get_text(strip=True)
        rows: List[Dict[str, str]] = []
        for tr in table.find_all("tr")[1:]:
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) >= 5:
                player, pos, date, injury, status = tds[:5]
                rows.append({"player": player, "pos": pos, "status": status, "injury": injury})
        if rows:
            result[_normalize_team(team_name)] = rows
    return result


def fetch_game_injuries(league: str, game_id: str) -> Dict[str, List[Dict[str, str]]]:
    """Scrape injury tables from a specific game page. Best effort per league."""
    if not game_id:
        return {}
    league_paths = {
        "NBA": "nba",
        "NCAAM": "mens-college-basketball",
        "NFL": "nfl",
        "NCAAF": "college-football",
        "NHL": "nhl",
        "MLB": "mlb",
    }
    path = league_paths.get(league)
    if not path:
        return {}
    url = f"https://www.espn.com/{path}/game/_/gameId/{game_id}"
    try:
        tables = pd.read_html(url)
    except Exception:
        return {}
    result: Dict[str, List[Dict[str, str]]] = {}
    for df in tables:
        cols = [str(c).lower() for c in df.columns]
        if not any("inj" in c for c in cols):
            continue
        for _, row in df.iterrows():
            player = str(row.get(df.columns[0], "")).strip()
            status = str(row.get("Status", row.get(df.columns[2], ""))).strip()
            injury = str(row.get("Injury", row.get(df.columns[-1], ""))).strip()
            team = str(row.get("Team", row.get(df.columns[-2], ""))).strip()
            if not team and len(df.columns) >= 5:
                team = str(row.get(df.columns[-2], "")).strip()
            if not team:
                continue
            entry = {"player": player, "status": status, "injury": injury}
            result.setdefault(_normalize_team(team), []).append(entry)
    return result


# ─── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Market movement scanner from ESPN odds page (open vs current)."
    )
    parser.add_argument("--spread-threshold", type=float, default=None)
    parser.add_argument("--total-threshold", type=float, default=None)
    parser.add_argument("--moneyline-threshold", type=float, default=None)
    return parser.parse_args()


def get_market_moves(
    leagues: Optional[List[str]] = None,
    spread_threshold: Optional[float] = None,
    total_threshold: Optional[float] = None,
    moneyline_threshold: Optional[float] = None,
) -> Dict[str, List[Tuple[Dict, List[Tuple[str, float]]]]]:
    allowed = set(leagues) if leagues else LEAGUE_ALLOWLIST
    html = fetch_odds_page()
    state = extract_state(html)

    league_to_events: Dict[str, List[Tuple[Dict, List[Tuple[str, float]]]]] = {}
    for league, line in iter_events(state):
        if league not in allowed:
            continue
        defaults = SPORT_DEFAULTS.get(league, {"spread": 1.0, "total": 1.5, "ml_prob": 0.05})
        s_thr = spread_threshold if spread_threshold is not None else defaults["spread"]
        t_thr = total_threshold if total_threshold is not None else defaults["total"]
        ml_thr = moneyline_threshold if moneyline_threshold is not None else defaults["ml_prob"]
        picks = collect_market_moves(league, line, s_thr, t_thr, ml_thr)
        if not picks:
            continue
        league_to_events.setdefault(league, []).append((line, picks))
    return league_to_events


def main() -> None:
    args = parse_args()
    league_to_events = get_market_moves(
        leagues=list(LEAGUE_ALLOWLIST),
        spread_threshold=args.spread_threshold,
        total_threshold=args.total_threshold,
        moneyline_threshold=args.moneyline_threshold,
    )

    if not league_to_events:
        print("No market-move candidates found.")
        return

    injuries_by_league = {lg: fetch_injuries(lg) for lg in league_to_events.keys()}

    for league, events in league_to_events.items():
        print(f"\n=== {league} ===")
        for line, picks in events:
            competitors = line.get("competitors", [])
            home = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away = next((c for c in competitors if c.get("homeAway") == "away"), {})
            home_team = home.get("team", {}).get("displayName", "Home")
            away_team = away.get("team", {}).get("displayName", "Away")
            start = iso_to_local_text(line.get("date", ""))
            print(f"- {away_team} @ {home_team} — {start}")
            league_inj = injuries_by_league.get(league, {})
            game_inj = fetch_game_injuries(league, line.get("id", ""))
            home_inj = game_inj.get(_normalize_team(home_team), []) or league_inj.get(
                _normalize_team(home_team), []
            )
            away_inj = game_inj.get(_normalize_team(away_team), []) or league_inj.get(
                _normalize_team(away_team), []
            )
            home_inj = home_inj[:3]
            away_inj = away_inj[:3]
            if home_inj or away_inj:
                parts = []
                if away_inj:
                    parts.append(
                        f"{away_team} injuries: "
                        + "; ".join(f"{r['player']} ({r['status']})" for r in away_inj)
                    )
                if home_inj:
                    parts.append(
                        f"{home_team} injuries: "
                        + "; ".join(f"{r['player']} ({r['status']})" for r in home_inj)
                    )
                print(f"    Injuries: {' | '.join(parts)}")
            for pick_text, score in picks:
                label = confidence_label(score)
                print(f"    • [{label}] Signal: {score*100:.0f}% — {pick_text}")


if __name__ == "__main__":
    main()
