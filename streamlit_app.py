import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
from typing import Optional, Dict, List, Tuple
from datetime import datetime, timezone, timedelta

from contrarian import (
    LEAGUE_ALLOWLIST,
    DK_LEAGUE_LINKS,
    collect_market_moves,
    compute_rest_days,
    confidence_label,
    evaluate_weather_impact,
    extract_state,
    fetch_action_network,
    fetch_covers_consensus,
    fetch_injuries,
    fetch_odds_page,
    fetch_recent_games,
    fetch_weather,
    get_bet_pcts,
    get_stadium_coords,
    iso_to_local_text,
    iter_events,
    match_an_game,
    match_covers_game,
    parse_game_dt,
    rest_day_note,
    rlm_signal,
    _normalize_team,
    _pick_team,
    SPORT_DEFAULTS,
)

st.set_page_config(page_title="Contrarian Plays", page_icon="📉", layout="wide")

st.title("📉 Contrarian Plays Finder")
st.caption(
    "Detects sharp-money line movements using ESPN odds (open → current). "
    "**Built for DraftKings.** All data is free — no paid APIs."
)

# ─── How to use ───────────────────────────────────────────────────────────────
with st.expander("How to read the signals — read this first"):
    st.markdown(
        """
### What this tool does
It compares **opening lines** to **current lines** on ESPN. When a line moves
significantly with no obvious news (injury, weather, lineup), that move was
likely driven by sharp (professional) money. This tool flags those games.

### Signal tiers — what they mean
| Icon | Tier | Signal Score | Meaning |
|------|------|-------------|---------|
| 🔒🔒 | **MULTI-SIGNAL LOCK** | — | Spread AND moneyline both crossed thresholds pointing to same team. Strongest possible signal. |
| 🔒 | **LOCK** | ≥ 75% | Large move + convergence/key number. Multiple signals agree. |
| ⚡ | **STRONG** | 50–74% | Meaningful move with partial confirmation. |
| ✅ | **LEAN** | 25–49% | Threshold crossed, single signal. |
| 👁 | **WATCH** | < 25% | Borderline — informational only, don't bet on this alone. |

> **Important:** The signal score is NOT a win probability. A "LOCK" does not
> mean you'll win — it means multiple independent signals agree. Sharp money
> is right about 54–58% of the time against the spread. Bankroll management matters.

### How to use it
1. Click **Find Contrarian Plays**
2. Filter by tier (start with LOCK and STRONG only)
3. Check the injury flags — if an injury explains the move, **skip that game**
4. Check **public betting %** (Covers.com) — prefer plays where < 45% of public is on your side (RLM)
5. Check **weather** (Open-Meteo) for NFL/MLB outdoor games — wind ≥ 15 mph or rain → under lean
6. Check **rest days** — NBA back-to-backs and NFL short weeks add real edge
7. Click **Bet on DraftKings** to go directly to the league on DK

### Free data sources used
| Source | What it provides | Cost |
|---|---|---|
| ESPN odds page | Open vs current lines — the core signal | Free (scrape) |
| Open-Meteo | Weather forecast for outdoor NFL/MLB stadiums | Free, no key |
| Covers.com | Public betting % (tickets + money per side) | Free (scrape) |
| ESPN schedule API | Rest days per team (back-to-backs, bye weeks) | Free, no key |
| ESPN injuries | OUT/DOUBTFUL/QUESTIONABLE players | Free (scrape) |

### Key rules
- ✅ Follow moves with no obvious news explanation
- ⚠️ If injury/goalie/pitcher explains the move → pass
- ⚠️ If the line moved WITH the public (no RLM) → less convincing
- ❌ Never bet more than 1–3% of your bankroll per game
- 🔒🔒 Multi-signal plays are the highest priority — spread AND ML agree

### Score penalty
Score is **reduced by 20%** when spread and ML move in **opposite** directions (conflict signal).
        """
    )

# ─── Helper rendering functions ───────────────────────────────────────────────

def _pick_icon(score: float) -> str:
    if score >= 0.75:
        return "🔒"
    if score >= 0.50:
        return "⚡"
    if score >= 0.25:
        return "✅"
    return "👁"


def _render_pick(pick_text: str, score: float) -> None:
    icon = _pick_icon(score)
    label = confidence_label(score)
    is_multi = pick_text.startswith("[MULTI-SIGNAL]")
    display_text = pick_text.replace("[MULTI-SIGNAL] ", "")
    signal_pct = f"{score*100:.0f}%"
    if is_multi:
        st.markdown(
            f"- 🔒🔒 **[MULTI-SIGNAL LOCK — Signal: {signal_pct}]** {display_text}"
        )
    else:
        st.markdown(f"- {icon} **[{label} — Signal: {signal_pct}]** {display_text}")


# ─── Position impact tiers ────────────────────────────────────────────────────

_POS_TIERS: Dict[str, Dict[str, int]] = {
    "NFL": {
        "QB": 1,
        "RB": 2, "WR": 2, "TE": 2, "OT": 2, "OG": 2, "G": 2, "OL": 2,
        "CB": 3, "S": 3, "LB": 3, "DE": 3, "DT": 3, "DB": 3, "DL": 3, "NT": 3,
        "K": 4, "P": 4, "LS": 4,
    },
    "NCAAF": {
        "QB": 1,
        "RB": 2, "WR": 2, "TE": 2, "OT": 2, "OG": 2, "G": 2, "OL": 2,
        "CB": 3, "S": 3, "LB": 3, "DE": 3, "DT": 3, "DB": 3,
        "K": 4, "P": 4,
    },
    "NBA": {
        "PG": 2, "SG": 2, "SF": 2, "PF": 2, "C": 2,
        "G": 2, "F": 2, "G-F": 2, "F-G": 2, "F-C": 2, "C-F": 2,
    },
    "NCAAM": {
        "PG": 2, "SG": 2, "SF": 2, "PF": 2, "C": 2,
        "G": 2, "F": 2,
    },
    "NHL": {
        "G": 1,
        "C": 2, "LW": 2, "RW": 2, "W": 2, "F": 2,
        "D": 3, "LD": 3, "RD": 3,
    },
    "MLB": {
        "SP": 1,
        "C": 2, "3B": 2, "SS": 2, "CF": 2,
        "1B": 3, "2B": 3, "LF": 3, "RF": 3, "OF": 3, "DH": 3,
        "RP": 4, "CL": 4, "MR": 4,
    },
}


def _pos_tier(pos: str, league: str) -> int:
    return _POS_TIERS.get(league, {}).get(pos.upper().strip(), 3)


def _significant_injuries(inj_list: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return [r for r in inj_list if any(
        kw in r.get("status", "").upper() for kw in ("OUT", "DOUBT")
    )]


def _injury_caution(
    pick_text: str,
    home_team: str,
    away_team: str,
    home_inj: List[Dict[str, str]],
    away_inj: List[Dict[str, str]],
    league: str = "",
) -> Optional[str]:
    clean = pick_text.replace("[MULTI-SIGNAL] ", "")

    if clean.startswith("FOLLOW: Over") or clean.startswith("FOLLOW: Under"):
        parts = []
        has_star = False
        for team, inj in ((away_team, away_inj), (home_team, home_inj)):
            sig = _significant_injuries(inj)
            if not sig:
                continue
            sig.sort(key=lambda r: _pos_tier(r.get("pos", ""), league))
            if any(_pos_tier(r.get("pos", ""), league) == 1 for r in sig):
                has_star = True
            names = ", ".join(
                f"{r['player']} ({r.get('pos', '')})" if r.get("pos") else r["player"]
                for r in sig[:2]
            )
            parts.append(f"{team}: {names}")
        if parts:
            prefix = "🚨 STAR PLAYER injury flag" if has_star else "⚠️ Injury flag"
            return (
                f"{prefix} — key players OUT/DOUBTFUL: "
                + " | ".join(parts)
                + ". Verify the total move isn't injury-driven before playing."
            )
        return None

    followed = _pick_team(clean, home_team, away_team)
    if followed is None:
        return None
    opposing_team = away_team if followed == home_team else home_team
    opposing_inj = away_inj if followed == home_team else home_inj
    sig = _significant_injuries(opposing_inj)
    if sig:
        sig.sort(key=lambda r: _pos_tier(r.get("pos", ""), league))
        has_star = any(_pos_tier(r.get("pos", ""), league) == 1 for r in sig)
        names = ", ".join(
            f"{r['player']} ({r.get('pos', '')})" if r.get("pos") else r["player"]
            for r in sig[:2]
        )
        extra = f" +{len(sig) - 2} more" if len(sig) > 2 else ""
        prefix = "🚨 STAR PLAYER injury flag" if has_star else "⚠️ Injury flag"
        return (
            f"{prefix} — {opposing_team} has {names}{extra} OUT/DOUBTFUL. "
            f"Line move may be injury-driven, not sharp money — verify before playing."
        )
    return None


def _render_inj_row(team: str, inj_list: List[Dict[str, str]], league: str = "") -> str:
    sig = _significant_injuries(inj_list)
    quest = [r for r in inj_list if "QUEST" in r.get("status", "").upper()]
    sig.sort(key=lambda r: _pos_tier(r.get("pos", ""), league))
    quest.sort(key=lambda r: _pos_tier(r.get("pos", ""), league))
    parts = []
    for r in sig:
        tier = _pos_tier(r.get("pos", ""), league)
        star = "⭐" if tier == 1 else ""
        pos_tag = f" {r['pos']}" if r.get("pos") else ""
        parts.append(f"{star}🔴 {r['player']}{pos_tag} ({r['status']})")
    for r in quest[:3]:
        tier = _pos_tier(r.get("pos", ""), league)
        star = "⭐" if tier == 1 else ""
        pos_tag = f" {r['pos']}" if r.get("pos") else ""
        parts.append(f"{star}🟡 {r['player']}{pos_tag} (Q)")
    return f"{team}: " + " · ".join(parts) if parts else ""


# ─── Data fetching (cached) ───────────────────────────────────────────────────

@st.cache_data(ttl=300)
def cached_state():
    html = fetch_odds_page()
    return extract_state(html)


@st.cache_data(ttl=900)
def cached_injuries(league: str):
    return fetch_injuries(league)


@st.cache_data(ttl=300)
def cached_action_network(sport: str) -> List[dict]:
    """Fetch Action Network bet % data (free, no API key). Returns [] on failure."""
    return fetch_action_network(sport)


@st.cache_data(ttl=300)
def cached_covers_consensus(league: str) -> List[dict]:
    """Scrape Covers.com public bet % (free HTML page). Returns [] on failure."""
    return fetch_covers_consensus(league)


@st.cache_data(ttl=600)
def cached_recent_games(league: str) -> dict:
    """Fetch ESPN scoreboard for past 8 days to compute rest days. Returns {} on failure."""
    return fetch_recent_games(league)


@st.cache_data(ttl=600)
def cached_weather(lat: float, lon: float, game_iso: str) -> Optional[dict]:
    """Fetch Open-Meteo forecast for the given coordinates and game time."""
    game_dt = parse_game_dt(game_iso)
    return fetch_weather(lat, lon, game_dt)


def game_url(league: str, game_id: str) -> Optional[str]:
    paths = {
        "NBA": "nba",
        "NCAAM": "mens-college-basketball",
        "NFL": "nfl",
        "NCAAF": "college-football",
        "NHL": "nhl",
        "MLB": "mlb",
    }
    path = paths.get(league)
    if path and game_id:
        return f"https://www.espn.com/{path}/game/_/gameId/{game_id}"
    return None


@st.cache_data(ttl=3600)
def fetch_team_stats(league: str):
    urls = {
        "NBA": {
            "off": "https://www.espn.com/nba/stats/team/_/view/offense",
            "def": "https://www.espn.com/nba/stats/team/_/view/defense",
        },
        "NCAAM": {
            "off": "https://www.espn.com/mens-college-basketball/stats/team/_/view/offense",
            "def": "https://www.espn.com/mens-college-basketball/stats/team/_/view/defense",
        },
        "NFL": {
            "off": "https://www.espn.com/nfl/stats/team/_/view/offense",
            "def": "https://www.espn.com/nfl/stats/team/_/view/defense",
        },
        "NCAAF": {
            "off": "https://www.espn.com/college-football/stats/team/_/view/offense",
            "def": "https://www.espn.com/college-football/stats/team/_/view/defense",
        },
        "NHL": {
            "off": "https://www.espn.com/nhl/stats/team",
            "def": "https://www.espn.com/nhl/stats/team/_/view/goalie",
        },
        "MLB": {
            "off": "https://www.espn.com/mlb/stats/team",
            "def": "https://www.espn.com/mlb/stats/team/_/view/pitching",
        },
    }
    if league not in urls:
        return {}
    stats = {}
    for kind in ("off", "def"):
        url = urls[league].get(kind)
        if not url:
            continue
        try:
            tables = pd.read_html(url)
            if not tables:
                continue
            df = tables[0]
            team_col = next((c for c in df.columns if "TEAM" in str(c).upper()), None)
            if not team_col:
                continue
            df = df.rename(columns={team_col: "TEAM"})
            for _, row in df.iterrows():
                team = row["TEAM"]
                key = _normalize_team(team)
                entry = stats.setdefault(key, {})
                for col in ("PTS", "R", "GF", "PTS/G"):
                    if col in row:
                        entry[f"{kind}_pts"] = row[col]
                        break
        except Exception:
            continue
    return stats


# ─── Controls ─────────────────────────────────────────────────────────────────

col1, col2, col3, col4 = st.columns([1.4, 1, 1, 1])
with col1:
    leagues = st.multiselect(
        "Leagues",
        options=sorted(LEAGUE_ALLOWLIST),
        default=sorted(LEAGUE_ALLOWLIST),
    )
with col2:
    spread_threshold = st.slider("Spread move ≥ (pts)", 0.5, 6.0, 2.0, 0.5)
with col3:
    total_threshold = st.slider("Total move ≥ (pts)", 1.0, 6.0, 3.0, 0.5)
with col4:
    moneyline_threshold = st.slider(
        "ML move ≥ (prob pts)", 0.01, 0.20, 0.08, 0.01
    )

tier_filter = st.radio(
    "Show plays at or above tier:",
    options=["All (WATCH+)", "LEAN+", "STRONG+", "LOCK only"],
    index=1,
    horizontal=True,
)

_tier_min_score = {"All (WATCH+)": 0.0, "LEAN+": 0.25, "STRONG+": 0.50, "LOCK only": 0.75}
min_score = _tier_min_score[tier_filter]

st.divider()
fetch = st.button("Find Contrarian Plays", type="primary", use_container_width=True)

# ─── Main results ─────────────────────────────────────────────────────────────

state = None
all_top_picks: List[Dict] = []  # for summary table

if fetch:
    with st.spinner("Fetching ESPN odds and checking for line movements..."):
        try:
            html = fetch_odds_page()
            state = extract_state(html)
        except Exception as exc:
            st.error(f"Failed to fetch ESPN odds: {exc}")
            st.stop()

    results = {}
    an_data: Dict[str, List[dict]] = {}      # league → Action Network games
    covers_data: Dict[str, List[dict]] = {}  # league → Covers.com consensus
    recent_games: Dict[str, dict] = {}       # league → team→last_game_dt

    if state:
        # Pre-fetch all free external data sources in parallel via caching
        for lg in leagues:
            # Bet % sources (Action Network first, fall back to Covers)
            an_games = cached_action_network(lg)
            if an_games:
                an_data[lg] = an_games
            cov = cached_covers_consensus(lg)
            if cov:
                covers_data[lg] = cov
            # Rest-day data (ESPN schedule API — free, no auth)
            rg = cached_recent_games(lg)
            if rg:
                recent_games[lg] = rg

        for league, line in iter_events(state):
            if league not in leagues:
                continue
            league_inj = cached_injuries(league)
            picks = collect_market_moves(
                league,
                line,
                spread_threshold=spread_threshold,
                total_threshold=total_threshold,
                moneyline_threshold=moneyline_threshold,
            )
            filtered = [(t, s) for t, s in picks if s >= min_score]
            if filtered:
                results.setdefault(league, []).append((line, filtered, league_inj))

    # ── Today's Best Plays summary ─────────────────────────────────────────
    # Collect all qualifying picks across all leagues for the summary table
    for league, events in results.items():
        for line, picks, league_inj in events:
            competitors = line.get("competitors", [])
            home = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away = next((c for c in competitors if c.get("homeAway") == "away"), {})
            home_team = home.get("team", {}).get("displayName", "Home")
            away_team = away.get("team", {}).get("displayName", "Away")
            start = iso_to_local_text(line.get("date", ""))
            for pick_text, score in picks:
                if score >= 0.50:  # only LOCK and STRONG in summary
                    is_multi = pick_text.startswith("[MULTI-SIGNAL]")
                    display = pick_text.replace("[MULTI-SIGNAL] ", "")
                    tier = "🔒🔒 MULTI-SIGNAL" if is_multi else (
                        "🔒 LOCK" if score >= 0.75 else "⚡ STRONG"
                    )
                    all_top_picks.append({
                        "Tier": tier,
                        "Signal": f"{score*100:.0f}%",
                        "Game": f"{away_team} @ {home_team}",
                        "Time": start,
                        "Play": display.replace("FOLLOW: ", ""),
                        "League": league,
                    })

    if all_top_picks:
        st.subheader("Today's Best Plays (LOCK + STRONG)")
        st.caption(
            "Signal % = how many independent signals agree and how large the move was. "
            "**Not a win probability.** Sharp money wins ~54–58% ATS long-term."
        )
        df_summary = pd.DataFrame(all_top_picks)
        df_summary = df_summary.sort_values("Signal", ascending=False)
        st.dataframe(
            df_summary[["Tier", "Signal", "League", "Game", "Time", "Play"]],
            use_container_width=True,
            hide_index=True,
        )
        st.divider()
    elif results:
        st.info("No LOCK or STRONG plays found — try lowering the tier filter to see LEAN plays.")

    # ── Per-game detailed results ──────────────────────────────────────────
    if not results:
        st.info("No contrarian candidates found. Try lowering the thresholds or tier filter.")
    else:
        for league in sorted(results.keys()):
            dk_link = DK_LEAGUE_LINKS.get(league)
            col_hdr, col_dk = st.columns([4, 1])
            with col_hdr:
                st.header(league)
            with col_dk:
                if dk_link:
                    st.link_button(
                        f"Bet {league} on DraftKings →",
                        dk_link,
                        use_container_width=True,
                    )

            an_games = an_data.get(league, [])
            an_available = bool(an_games)

            for line, picks, league_inj in results[league]:
                competitors = line.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})
                home_team = home.get("team", {}).get("displayName", "Home")
                away_team = away.get("team", {}).get("displayName", "Away")
                game_iso  = line.get("date", "")
                start     = iso_to_local_text(game_iso)
                game_dt   = parse_game_dt(game_iso)

                st.subheader(f"{away_team} @ {home_team}")
                st.caption(start)

                # Links row
                link_cols = st.columns([1, 1, 2])
                espn_link = game_url(league, line.get("id", ""))
                if espn_link:
                    link_cols[0].caption(f"[ESPN game page]({espn_link})")
                if dk_link:
                    link_cols[1].caption(f"[Open on DraftKings]({dk_link})")

                # ── Weather (Open-Meteo — completely free, no API key) ───────
                stadium_coords = get_stadium_coords(home_team, league)
                weather_note: Optional[str] = None
                if stadium_coords:
                    weather = cached_weather(
                        stadium_coords[0], stadium_coords[1], game_iso
                    )
                    weather_note = evaluate_weather_impact(weather)
                    if weather_note:
                        st.caption(weather_note)
                    elif stadium_coords:
                        # Outdoor stadium but weather is fine — mention it briefly
                        if weather and weather.get("temp_f") is not None:
                            wt = weather['temp_f']
                            ww = weather.get('wind_mph') or 0
                            st.caption(
                                f"🌤️ Weather: {wt:.0f}°F, {ww:.0f} mph wind — no impact on total"
                            )

                # ── Public bet % (Covers.com primary, Action Network fallback) ─
                c_games = covers_data.get(league, [])
                a_games = an_data.get(league, [])
                covers_game = match_covers_game(home_team, away_team, c_games) if c_games else None
                an_game     = match_an_game(home_team, away_team, a_games) if a_games else None

                bet_pcts: Dict = {}
                bet_source = ""
                if covers_game:
                    # Covers returns home_pct/away_pct directly
                    bet_pcts = {
                        "spread_home_pct": covers_game.get("home_pct"),
                        "spread_away_pct": covers_game.get("away_pct"),
                        "money_home_pct":  covers_game.get("home_money_pct"),
                        "money_away_pct":  covers_game.get("away_money_pct"),
                    }
                    bet_source = "Covers.com"
                elif an_game:
                    bet_pcts = get_bet_pcts(an_game)
                    bet_source = "Action Network"

                if bet_pcts:
                    pct_parts = []
                    sh = bet_pcts.get("spread_home_pct")
                    sa = bet_pcts.get("spread_away_pct")
                    if sh is not None and sa is not None:
                        pct_parts.append(
                            f"Spread: {home_team} **{sh}%** / {away_team} **{sa}%**"
                        )
                    mh = bet_pcts.get("money_home_pct") or bet_pcts.get("money_home_pct")
                    ma = bet_pcts.get("money_away_pct")
                    if mh is not None and ma is not None:
                        pct_parts.append(f"Money: {home_team} {mh}% / {away_team} {ma}%")
                    ov = bet_pcts.get("over_pct")
                    un = bet_pcts.get("under_pct")
                    if ov is not None and un is not None:
                        pct_parts.append(f"O/U: Over **{ov}%** / Under **{un}%**")
                    if pct_parts:
                        st.caption(f"📊 Public betting ({bet_source}): " + " | ".join(pct_parts))

                # ── Rest days (ESPN schedule API — free, no auth) ─────────────
                lg_recent = recent_games.get(league, {})
                for team in (away_team, home_team):
                    days = compute_rest_days(team, lg_recent, game_dt)
                    note = rest_day_note(days, league)
                    if note:
                        st.caption(f"{team}: {note}")

                # ── Injury display ────────────────────────────────────────────
                home_inj = league_inj.get(_normalize_team(home_team), [])
                away_inj = league_inj.get(_normalize_team(away_team), [])
                inj_rows = [
                    _render_inj_row(t, inj, league)
                    for t, inj in ((away_team, away_inj), (home_team, home_inj))
                ]
                inj_rows = [r for r in inj_rows if r]
                if inj_rows:
                    st.caption(" | ".join(inj_rows))

                # ── Picks with RLM, weather confirmation, and injury cautions ─
                for pick_text, score in picks:
                    _render_pick(pick_text, score)

                    # Weather confirmation/warning for totals
                    clean_pick = pick_text.replace("[MULTI-SIGNAL] ", "")
                    if weather_note and (
                        clean_pick.startswith("FOLLOW: Under") or
                        clean_pick.startswith("FOLLOW: Over")
                    ):
                        is_under = clean_pick.startswith("FOLLOW: Under")
                        if is_under and "under lean" in weather_note.lower():
                            st.caption(
                                f"✅ Weather CONFIRMS this Under pick: {weather_note}"
                            )
                        elif not is_under and "under lean" in weather_note.lower():
                            st.caption(
                                f"⚠️ Weather CONFLICTS with this Over pick: {weather_note}"
                            )

                    # Reverse line movement signal
                    rlm = rlm_signal(pick_text, home_team, away_team, bet_pcts)
                    if rlm:
                        st.caption(rlm)

                    # Injury caution
                    caution = _injury_caution(
                        pick_text, home_team, away_team, home_inj, away_inj, league
                    )
                    if caution:
                        st.caption(caution)

                st.divider()

# ─── Totals with context ──────────────────────────────────────────────────────

st.divider()
st.header("Totals Focus — with Team Stats Context")
st.caption(
    "Filters for Over/Under line moves only, then adds offensive/defensive scoring averages "
    "to help you decide if the total makes sense. Uses sport-specific thresholds."
)

defaults_totals = {
    "NBA": 1.5, "NCAAM": 1.5,
    "NHL": 0.5,
    "NFL": 2.0, "NCAAF": 2.0,
    "MLB": 1.5,
}
t_cols = st.columns(4)
nba_thr = t_cols[0].slider("NBA/NCAAM ≥", 0.5, 6.0, defaults_totals["NBA"], 0.5, key="ctx_nba")
nhl_thr = t_cols[1].slider("NHL ≥",       0.25, 3.0, defaults_totals["NHL"], 0.25, key="ctx_nhl")
nfl_thr = t_cols[2].slider("NFL/NCAAF ≥", 0.5, 8.0, defaults_totals["NFL"], 0.5,  key="ctx_nfl")
mlb_thr = t_cols[3].slider("MLB ≥",       0.5, 5.0, defaults_totals["MLB"], 0.5,  key="ctx_mlb")

sport_total_thresholds = {
    "NBA": nba_thr, "NCAAM": nba_thr,
    "NHL": nhl_thr,
    "NFL": nfl_thr, "NCAAF": nfl_thr,
    "MLB": mlb_thr,
}

if st.button("Find Totals with Context", use_container_width=True):
    with st.spinner("Fetching odds, team stats, and injuries..."):
        try:
            ctx_state = cached_state()
        except Exception as exc:
            st.error(f"Failed to fetch odds: {exc}")
            st.stop()

        leagues_seen = {league for league, _ in iter_events(ctx_state)}
        stats_cache = {lg: fetch_team_stats(lg) for lg in leagues_seen}
        inj_cache   = {lg: cached_injuries(lg) for lg in leagues_seen}

    totals_results: Dict[str, list] = {}
    for league, line in iter_events(ctx_state):
        if league not in sport_total_thresholds:
            continue
        league_inj = inj_cache.get(league, {})
        picks = collect_market_moves(
            league,
            line,
            spread_threshold=1e6,
            total_threshold=sport_total_thresholds[league],
            moneyline_threshold=1.0,
        )
        total_picks = [
            (t, s) for t, s in picks
            if t.startswith("FOLLOW: Over") or t.startswith("FOLLOW: Under")
        ]
        if total_picks:
            totals_results.setdefault(league, []).append((line, total_picks, league_inj))

    if not totals_results:
        st.info("No totals moves met the thresholds.")
    else:
        for league in sorted(totals_results.keys()):
            st.subheader(league)
            dk_link = DK_LEAGUE_LINKS.get(league)
            if dk_link:
                st.link_button(f"Bet {league} totals on DraftKings →", dk_link)

            stats = stats_cache.get(league, {})
            injuries = inj_cache.get(league, {})
            for line, picks, league_inj in totals_results[league]:
                competitors = line.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})
                home_team = home.get("team", {}).get("displayName", "Home")
                away_team = away.get("team", {}).get("displayName", "Away")
                start = iso_to_local_text(line.get("date", ""))

                st.markdown(f"**{away_team} @ {home_team}** — {start}")

                for pick_text, score in picks:
                    _render_pick(pick_text, score)

                # Team stats context
                home_stats = stats.get(_normalize_team(home_team), {})
                away_stats = stats.get(_normalize_team(away_team), {})
                stat_line = []
                if home_stats.get("off_pts") and home_stats.get("def_pts"):
                    stat_line.append(
                        f"{home_team} off/def: {home_stats['off_pts']}/{home_stats['def_pts']}"
                    )
                if away_stats.get("off_pts") and away_stats.get("def_pts"):
                    stat_line.append(
                        f"{away_team} off/def: {away_stats['off_pts']}/{away_stats['def_pts']}"
                    )
                if stat_line:
                    st.caption("📈 Stats: " + " • ".join(stat_line))

                # Injuries
                home_inj = injuries.get(_normalize_team(home_team), [])
                away_inj = injuries.get(_normalize_team(away_team), [])
                inj_rows = [
                    _render_inj_row(t, inj, league)
                    for t, inj in ((away_team, away_inj), (home_team, home_inj))
                ]
                inj_rows = [r for r in inj_rows if r]
                if inj_rows:
                    st.caption(" | ".join(inj_rows))

                for pick_text, _ in picks:
                    caution = _injury_caution(
                        pick_text, home_team, away_team, home_inj, away_inj, league
                    )
                    if caution:
                        st.caption(caution)
                        break

                best = picks[0][0]
                rec = "Lean Over" if best.startswith("FOLLOW: Over") else "Lean Under"
                st.badge(rec)
                st.divider()

else:
    if not fetch:
        st.info('Set your thresholds above and click **"Find Contrarian Plays"** to get started.')
