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
