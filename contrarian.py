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

# Sport-specific defaults to avoid over/under-filtering moves.
SPORT_DEFAULTS = {
    "NBA": {"spread": 1.0, "total": 1.5, "ml_prob": 0.05},
    "NCAAM": {"spread": 1.0, "total": 1.5, "ml_prob": 0.05},
    "NFL": {"spread": 0.5, "total": 1.5, "ml_prob": 0.04},
    "NCAAF": {"spread": 0.5, "total": 1.5, "ml_prob": 0.04},
    "NHL": {"spread": 1.0, "total": 0.5, "ml_prob": 0.04},  # puck line mostly ±1.5; total moves in goals
    "MLB": {"spread": 1.0, "total": 1.0, "ml_prob": 0.04},  # run line ±1.5; totals tighter
}


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
        return dt.strftime("%Y-%m-%d %I:%M %p UTC")
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


def collect_market_moves(
    league: str,
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

    # Spreads (follow the move; price-only for NHL/MLB run/puck lines)
    spread = odds.get("pointSpread")
    if spread:
        home_open = parse_point(spread.get("home", {}).get("open", {}).get("line"))
        home_cur = parse_point(spread.get("home", {}).get("close", {}).get("line"))
        away_open = parse_point(spread.get("away", {}).get("open", {}).get("line"))
        away_cur = parse_point(spread.get("away", {}).get("close", {}).get("line"))
        home_open_price = american_to_prob(spread.get("home", {}).get("open", {}).get("odds"))
        home_cur_price = american_to_prob(spread.get("home", {}).get("close", {}).get("odds"))
        if None not in (home_open, home_cur, away_open, away_cur):
            # NHL/MLB: spreads rarely move off ±1.5; use price shift primarily.
            if league in {"NHL", "MLB"} and abs(home_open) == 1.5 and abs(home_cur) == 1.5:
                if home_open_price is not None and home_cur_price is not None:
                    price_delta = home_cur_price - home_open_price
                    if abs(price_delta) >= moneyline_threshold:
                        if price_delta > 0:
                            picks.append(
                                f"FOLLOW: {home_team} {home_cur:+} — price firmed by {price_delta*100:+.1f}pp at same line"
                            )
                        else:
                            picks.append(
                                f"FOLLOW: {away_team} {away_cur:+} — price firmed by {-price_delta*100:+.1f}pp at same line"
                            )
                # skip point-based logic for puck/run lines
            else:
                delta_line = home_cur - home_open
                delta_mag = abs(home_cur) - abs(home_open)  # toward/away from 0
                delta = delta_mag
                # If the line itself didn't move meaningfully, fall back to price shift.
                price_delta = None
                if home_open_price is not None and home_cur_price is not None:
                    price_delta = home_cur_price - home_open_price
                if delta <= -spread_threshold:
                    picks.append(
                        f"FOLLOW: {home_team} {home_cur:+} (was {home_open:+}) — line shift {delta_line:+.1f}"
                    )
                elif delta >= spread_threshold:
                    picks.append(
                        f"FOLLOW: {away_team} {away_cur:+} (was {away_open:+}) — line shift {delta_line:+.1f}"
                    )
                elif (
                    price_delta is not None
                    and abs(price_delta) >= moneyline_threshold
                    and home_cur == home_open
                ):
                    if price_delta > 0:
                        picks.append(
                            f"FOLLOW: {home_team} {home_cur:+} — price firmed by {price_delta*100:+.1f}pp at same line"
                        )
                    else:
                        picks.append(
                            f"FOLLOW: {away_team} {away_cur:+} — price firmed by {-price_delta*100:+.1f}pp at same line"
                        )

    # Totals (follow-the-move)
    total = odds.get("total")
    if total:
        open_point = parse_point(total.get("over", {}).get("open", {}).get("line"))
        cur_point = parse_point(total.get("over", {}).get("close", {}).get("line"))
        if None not in (open_point, cur_point):
            delta = cur_point - open_point
            if delta >= total_threshold:
                picks.append(
                    f"FOLLOW: Over {cur_point:.1f} — total moved {delta:+.1f} from {open_point:.1f}"
                )
            elif delta <= -total_threshold:
                picks.append(
                    f"FOLLOW: Under {cur_point:.1f} — total moved {delta:+.1f} from {open_point:.1f}"
                )

    # Moneylines (follow-the-move)
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
                    f"FOLLOW: {home_team} ML {moneyline.get('home', {}).get('close', {}).get('odds')} "
                    f"— market boosted home by {swing:+.1f}pp"
                )
            elif delta <= -moneyline_threshold:
                swing = -delta * 100
                picks.append(
                    f"FOLLOW: {away_team} ML {moneyline.get('away', {}).get('close', {}).get('odds')} "
                    f"— market boosted away by {swing:+.1f}pp"
                )

    return picks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Market movement scanner from ESPN odds page (open vs current)."
    )
    parser.add_argument(
        "--spread-threshold",
        type=float,
        default=None,
        help="Min spread move (points) to flag a side. Default uses sport-specific thresholds.",
    )
    parser.add_argument(
        "--total-threshold",
        type=float,
        default=None,
        help="Min total move (points) to flag a side. Default uses sport-specific thresholds.",
    )
    parser.add_argument(
        "--moneyline-threshold",
        type=float,
        default=None,
        help="Min implied probability change to flag a side (e.g., 0.05 = 5pp). Default uses sport-specific thresholds.",
    )
    return parser.parse_args()


def get_market_moves(
    leagues: Optional[List[str]] = None,
    spread_threshold: Optional[float] = None,
    total_threshold: Optional[float] = None,
    moneyline_threshold: Optional[float] = None,
) -> Dict[str, List[Tuple[Dict, List[str]]]]:
    allowed = set(leagues) if leagues else LEAGUE_ALLOWLIST
    html = fetch_odds_page()
    state = extract_state(html)

    league_to_events: Dict[str, List[Tuple[Dict, List[str]]]] = {}
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
