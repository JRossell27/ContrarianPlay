import argparse
import json
import re
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

import requests

ESPN_ODDS_URL = "https://www.espn.com/sports-betting/odds"
LEAGUE_ALLOWLIST = {
    "NBA",
    "NHL",
    "MLB",
    "NFL",
    "NCAAF",
    "NCAAM",
}  # NCAAM = men's college basketball on ESPN


def fetch_odds_page() -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(ESPN_ODDS_URL, headers=headers, timeout=30)
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
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %I:%M %p %Z")
    except Exception:
        return iso_str


def iter_events(state: Dict) -> Iterable[Tuple[str, Dict]]:
    odds_block = state.get("page", {}).get("content", {}).get("odds", {})
    for league_block in odds_block.get("odds", []):
        league = league_block.get("displayValue")
        if league not in LEAGUE_ALLOWLIST:
            continue
        for line in league_block.get("lines", []):
            yield league, line


def collect_contrarian_picks(
    line: Dict,
    spread_threshold: float,
    total_threshold: float,
    moneyline_threshold: float,
) -> List[str]:
    picks: List[str] = []

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

    # Moneyline first to reuse probability delta as confirmation signal.
    moneyline = odds.get("moneyline")
    home_ml_delta: Optional[float] = None
    if moneyline:
        home_open = american_to_prob(moneyline.get("home", {}).get("open", {}).get("odds"))
        home_cur = american_to_prob(moneyline.get("home", {}).get("close", {}).get("odds"))
        away_open = american_to_prob(moneyline.get("away", {}).get("open", {}).get("odds"))
        away_cur = american_to_prob(moneyline.get("away", {}).get("close", {}).get("odds"))
        if None not in (home_open, home_cur, away_open, away_cur):
            home_ml_delta = home_cur - home_open

    # Spreads
    spread = odds.get("pointSpread")
    if spread:
        home_open = parse_point(spread.get("home", {}).get("open", {}).get("line"))
        home_cur = parse_point(spread.get("home", {}).get("close", {}).get("line"))
        away_open = parse_point(spread.get("away", {}).get("open", {}).get("line"))
        away_cur = parse_point(spread.get("away", {}).get("close", {}).get("line"))
        home_open_price = american_to_prob(spread.get("home", {}).get("open", {}).get("odds"))
        home_cur_price = american_to_prob(spread.get("home", {}).get("close", {}).get("odds"))
        if None not in (home_open, home_cur, away_open, away_cur):
            # Puck/run line guard: if both sides are +/-1.5 and simply flipped, treat as noise.
            symmetrical_flip = (
                abs(home_open) == abs(home_cur) == abs(away_open) == abs(away_cur) == 1.5
                and home_open == -away_open
                and home_cur == -away_cur
                and home_open == -home_cur
            )
            if symmetrical_flip:
                delta_points = home_cur - home_open
                prob_shift = None
                if None not in (home_open_price, home_cur_price):
                    prob_shift = home_cur_price - home_open_price
                # Only accept the flip if there is a real price move AND ML moves the same way.
                if (
                    prob_shift is not None
                    and home_ml_delta is not None
                    and abs(prob_shift) >= moneyline_threshold
                    and prob_shift * home_ml_delta > 0
                ):
                    delta = delta_points
                else:
                    delta = 0.0
            else:
                delta = home_cur - home_open
            if delta <= -spread_threshold:
                # Steam toward home favorite; grab away with extra points.
                picks.append(
                    f"{away_team} {away_cur:+} (spread moved {delta:.1f} from {home_open:+} on {home_team})"
                )
            elif delta >= spread_threshold:
                # Steam toward away; grab home with better number.
                picks.append(
                    f"{home_team} {home_cur:+} (spread moved +{delta:.1f} from {home_open:+} vs {away_team})"
                )

    # Totals
    total = odds.get("total")
    if total:
        open_point = parse_point(total.get("over", {}).get("open", {}).get("line"))
        cur_point = parse_point(total.get("over", {}).get("close", {}).get("line"))
        if None not in (open_point, cur_point):
            delta = cur_point - open_point
            if delta >= total_threshold:
                picks.append(f"Under {cur_point:.1f} (total up {delta:.1f} from {open_point:.1f})")
            elif delta <= -total_threshold:
                picks.append(f"Over {cur_point:.1f} (total down {delta:.1f} from {open_point:.1f})")

    # Moneylines
    if moneyline:
        home_open = american_to_prob(moneyline.get("home", {}).get("open", {}).get("odds"))
        home_cur = american_to_prob(moneyline.get("home", {}).get("close", {}).get("odds"))
        away_open = american_to_prob(moneyline.get("away", {}).get("open", {}).get("odds"))
        away_cur = american_to_prob(moneyline.get("away", {}).get("close", {}).get("odds"))
        if None not in (home_open, home_cur, away_open, away_cur):
            delta = home_cur - home_open
            if delta >= moneyline_threshold:
                swing = delta * 100
                picks.append(
                    f"{away_team} ML {moneyline.get('away', {}).get('close', {}).get('odds')} "
                    f"(fade steam on {home_team}, +{swing:.1f}pp)"
                )
            elif delta <= -moneyline_threshold:
                swing = -delta * 100
                picks.append(
                    f"{home_team} ML {moneyline.get('home', {}).get('close', {}).get('odds')} "
                    f"(fade steam on {away_team}, +{swing:.1f}pp)"
                )

    return picks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Daily contrarian plays from ESPN odds page (open vs current)."
    )
    parser.add_argument(
        "--spread-threshold",
        type=float,
        default=2.0,
        help="Min spread move (points) to flag a contrarian side. Default 2.0 for higher confidence.",
    )
    parser.add_argument(
        "--total-threshold",
        type=float,
        default=3.0,
        help="Min total move (points) to flag a contrarian side. Default 3.0 to avoid noise.",
    )
    parser.add_argument(
        "--moneyline-threshold",
        type=float,
        default=0.08,
        help="Min implied probability change to flag a contrarian side (0.08=8pp).",
    )
    return parser.parse_args()


def get_contrarian_picks(
    leagues: Optional[List[str]] = None,
    spread_threshold: float = 2.0,
    total_threshold: float = 3.0,
    moneyline_threshold: float = 0.08,
) -> Dict[str, List[Tuple[Dict, List[str]]]]:
    allowed = set(leagues) if leagues else LEAGUE_ALLOWLIST
    html = fetch_odds_page()
    state = extract_state(html)

    league_to_events: Dict[str, List[Tuple[Dict, List[str]]]] = {}
    for league, line in iter_events(state):
        if league not in allowed:
            continue
        picks = collect_contrarian_picks(line, spread_threshold, total_threshold, moneyline_threshold)
        if not picks:
            continue
        league_to_events.setdefault(league, []).append((line, picks))
    return league_to_events


def main() -> None:
    args = parse_args()
    league_to_events = get_contrarian_picks(
        leagues=list(LEAGUE_ALLOWLIST),
        spread_threshold=args.spread_threshold,
        total_threshold=args.total_threshold,
        moneyline_threshold=args.moneyline_threshold,
    )

    if not league_to_events:
        print("No contrarian candidates found.")
        return

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
            for pick in picks:
                print(f"    • {pick}")


if __name__ == "__main__":
    main()
