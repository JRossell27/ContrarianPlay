import streamlit as st

from contrarian import (
    LEAGUE_ALLOWLIST,
    collect_market_moves,
    extract_state,
    fetch_injuries,
    fetch_game_injuries,
    fetch_odds_page,
    iso_to_local_text,
    iter_events,
    _normalize_team,
)

import pandas as pd
import requests
from bs4 import BeautifulSoup
from functools import lru_cache


st.set_page_config(page_title="Contrarian Plays", page_icon="📉", layout="wide")

st.title("📉 Contrarian Plays Finder")
st.caption(
    "Scrapes ESPN odds (open vs current) and highlights contrarian angles on spreads, totals, and moneylines."
)

with st.expander("How to read the signals (quick rules)"):
    st.markdown(
        """
- ✅ Only care about real moves: spreads ≥1.0pt, totals ≥1.5pts (NHL totals ≥0.5 goals), ML shifts ≳4% implied.
- ✅ Follow the move unless news explains it: Total ↓ → Under; favorite cheaper → underdog; ML favorite cheaper → look at dog.
- ⚠️ Contrarian requires no obvious news: if injury/goalie/pitcher explains the move, pass.
- ⚠️ Freezes/reversals matter: popular side not moving or a snap-back late can flip the lean.
- ❌ If you can’t explain the bet in one sentence (“Line moved ___ with no news, book protecting ___”), pass.
        """
    )


@st.cache_data(ttl=300)
def cached_state():
    html = fetch_odds_page()
    return extract_state(html)


@st.cache_data(ttl=900)
def cached_injuries(league: str):
    return fetch_injuries(league)


@st.cache_data(ttl=600)
def cached_game_injuries(league: str, game_id: str):
    return fetch_game_injuries(league, game_id)


def _normalize_team(name: str) -> str:
    return name.lower().replace(".", "").replace("'", "").strip()


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
            # Rename team column
            team_col = next((c for c in df.columns if "TEAM" in str(c).upper()), None)
            if not team_col:
                continue
            df = df.rename(columns={team_col: "TEAM"})
            for _, row in df.iterrows():
                team = row["TEAM"]
                key = _normalize_team(team)
                entry = stats.setdefault(key, {})
                if "PTS" in row:
                    entry[f"{kind}_pts"] = row["PTS"]
                elif "R" in row:
                    entry[f"{kind}_pts"] = row["R"]
                elif "GF" in row:
                    entry[f"{kind}_pts"] = row["GF"]
                if "PTS/G" in row:
                    entry[f"{kind}_pts"] = row["PTS/G"]
        except Exception:
            continue
    return stats


@st.cache_data(ttl=900)
def fetch_injuries(league: str):
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
    result = {}
    for section in soup.find_all("section"):
        header = section.find("h2")
        table = section.find("table")
        if not header or not table:
            continue
        team_name = header.get_text(strip=True)
        rows = []
        for tr in table.find_all("tr")[1:]:
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) >= 5:
                player, pos, date, injury, status = tds[:5]
                rows.append({"player": player, "pos": pos, "status": status, "injury": injury})
        if rows:
            result[_normalize_team(team_name)] = rows
    return result

col1, col2, col3, col4 = st.columns([1.2, 1, 1, 1])
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
        "Moneyline move ≥ (prob. points)", 0.01, 0.2, 0.08, 0.01
    )

st.divider()
fetch = st.button("Find contrarian plays", type="primary")

state = None
totals_results = {}
if fetch:
    with st.spinner("Fetching odds and crunching moves..."):
        try:
            html = fetch_odds_page()
            state = extract_state(html)
        except Exception as exc:
            st.error(f"Failed to fetch/process odds: {exc}")
            st.stop()

    results = {}
    if state:
        for league, line in iter_events(state):
            if league not in leagues:
                continue
            league_inj = cached_injuries(league)
            game_inj = cached_game_injuries(line.get("id", ""))
            picks = collect_market_moves(
                league,
                line,
                spread_threshold=spread_threshold,
                total_threshold=total_threshold,
                moneyline_threshold=moneyline_threshold,
            )
            if picks:
                results.setdefault(league, []).append((line, picks, league_inj, game_inj))

    if not results:
        st.info("No contrarian candidates found with the current thresholds.")
    else:
        for league in sorted(results.keys()):
            st.header(league)
            for line, picks, league_inj, game_inj in results[league]:
                competitors = line.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})
                home_team = home.get("team", {}).get("displayName", "Home")
                away_team = away.get("team", {}).get("displayName", "Away")
                start = iso_to_local_text(line.get("date", ""))
                st.subheader(f"{away_team} @ {home_team}")
                st.caption(start)
                # Injury snippets
                home_inj = game_inj.get(_normalize_team(home_team), []) or league_inj.get(_normalize_team(home_team), [])
                away_inj = game_inj.get(_normalize_team(away_team), []) or league_inj.get(_normalize_team(away_team), [])
                home_inj = home_inj[:3]
                away_inj = away_inj[:3]
                if home_inj or away_inj:
                    txt = []
                    if away_inj:
                        txt.append(
                            f"{away_team} injuries: "
                            + "; ".join(f"{r['player']} ({r['status']})" for r in away_inj)
                        )
                    if home_inj:
                        txt.append(
                            f"{home_team} injuries: "
                            + "; ".join(f"{r['player']} ({r['status']})" for r in home_inj)
                        )
                    st.caption(" | ".join(txt))
                for pick in picks:
                    st.markdown(f"- {pick}")
                st.divider()

if fetch and state:
    st.divider()
    st.header("Totals-only strategy")
    st.caption("Uses sport-specific thresholds to flag meaningful O/U moves.")

    defaults = {
        "NBA": 1.5,
        "NCAAM": 1.5,
        "NHL": 0.5,
        "NFL": 2.0,
        "NCAAF": 2.0,
        "MLB": 1.5,
    }
    cols = st.columns(3)
    nba_thresh = cols[0].slider("NBA / NCAAM total move ≥ (pts)", 0.5, 6.0, defaults["NBA"], 0.5)
    nhl_thresh = cols[1].slider("NHL total move ≥ (pts)", 0.25, 3.0, defaults["NHL"], 0.25)
    nfl_thresh = cols[2].slider("NFL / NCAAF total move ≥ (pts)", 0.5, 8.0, defaults["NFL"], 0.5)
    mlb_thresh = st.slider("MLB total move ≥ (pts)", 0.5, 5.0, defaults["MLB"], 0.5)

    sport_total_thresholds = {
        "NBA": nba_thresh,
        "NCAAM": nba_thresh,
        "NHL": nhl_thresh,
        "NFL": nfl_thresh,
        "NCAAF": nfl_thresh,
        "MLB": mlb_thresh,
    }

    for league, line in iter_events(state):
        if league not in sport_total_thresholds:
            continue
        league_inj = cached_injuries(league)
        game_inj = cached_game_injuries(line.get("id", ""))
        picks = collect_market_moves(
            league,
            line,
            spread_threshold=1e6,  # disable spread
            total_threshold=sport_total_thresholds[league],
            moneyline_threshold=1.0,  # disable ML
        )
        total_picks = [p for p in picks if p.startswith("FOLLOW: Over") or p.startswith("FOLLOW: Under")]
        if total_picks:
            totals_results.setdefault(league, []).append((line, total_picks, league_inj, game_inj))

    if not totals_results:
        st.info("No totals moves met the sport-specific thresholds.")
    else:
        for league in sorted(totals_results.keys()):
            st.subheader(league)
            for line, picks, league_inj, game_inj in totals_results[league]:
                competitors = line.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})
                home_team = home.get("team", {}).get("displayName", "Home")
                away_team = away.get("team", {}).get("displayName", "Away")
                start = iso_to_local_text(line.get("date", ""))
                st.markdown(f"**{away_team} @ {home_team}** — {start}")
                home_inj = game_inj.get(_normalize_team(home_team), []) or league_inj.get(_normalize_team(home_team), [])
                away_inj = game_inj.get(_normalize_team(away_team), []) or league_inj.get(_normalize_team(away_team), [])
                home_inj = home_inj[:3]
                away_inj = away_inj[:3]
                if home_inj or away_inj:
                    txt = []
                    if away_inj:
                        txt.append(
                            f"{away_team} injuries: "
                            + "; ".join(f"{r['player']} ({r['status']})" for r in away_inj)
                        )
                    if home_inj:
                        txt.append(
                            f"{home_team} injuries: "
                            + "; ".join(f"{r['player']} ({r['status']})" for r in home_inj)
                        )
                    st.caption(" | ".join(txt))
                for pick in picks:
                    st.markdown(f"- {pick}")
                st.divider()
else:
    st.info("Set your thresholds and click “Find contrarian plays”.")

# --- Totals + context page ---

st.divider()
st.header("Totals-only strategy with context")
st.caption(
    "Over/Under angles with sport-specific thresholds plus quick offense/defense stats and injury notes."
)

defaults = {
    "NBA": 1.5,
    "NCAAM": 1.5,
    "NHL": 0.5,
    "NFL": 2.0,
    "NCAAF": 2.0,
    "MLB": 1.5,
}
cols = st.columns(3)
nba_thresh = cols[0].slider("NBA / NCAAM total move ≥ (pts)", 0.5, 6.0, defaults["NBA"], 0.5)
nhl_thresh = cols[1].slider("NHL total move ≥ (pts)", 0.25, 3.0, defaults["NHL"], 0.25)
nfl_thresh = cols[2].slider("NFL / NCAAF total move ≥ (pts)", 0.5, 8.0, defaults["NFL"], 0.5)
mlb_thresh = st.slider("MLB total move ≥ (pts)", 0.5, 5.0, defaults["MLB"], 0.5)

if st.button("Find totals with context"):
    with st.spinner("Fetching odds, stats, and injuries..."):
        try:
            state = cached_state()
        except Exception as exc:
            st.error(f"Failed to fetch odds: {exc}")
            st.stop()

        # Preload stats/injuries per league encountered.
        leagues_seen = set([league for league, _ in iter_events(state)])
        stats_cache = {lg: fetch_team_stats(lg) for lg in leagues_seen}
        inj_cache = {lg: fetch_injuries(lg) for lg in leagues_seen}

    totals_results = {}
    sport_total_thresholds = {
        "NBA": nba_thresh,
        "NCAAM": nba_thresh,
        "NHL": nhl_thresh,
        "NFL": nfl_thresh,
        "NCAAF": nfl_thresh,
        "MLB": mlb_thresh,
    }

    for league, line in iter_events(state):
        if league not in sport_total_thresholds:
            continue
        league_inj = inj_cache.get(league, {})
        game_inj = cached_game_injuries(league, line.get("id", ""))
        picks = collect_market_moves(
            league,
            line,
            spread_threshold=1e6,  # disable spread
            total_threshold=sport_total_thresholds[league],
            moneyline_threshold=1.0,  # disable ML
        )
        total_picks = [p for p in picks if p.startswith("FOLLOW: Over") or p.startswith("FOLLOW: Under")]
        if total_picks:
            totals_results.setdefault(league, []).append((line, total_picks, league_inj, game_inj))

    if not totals_results:
        st.info("No totals moves met the sport-specific thresholds.")
    else:
        for league in sorted(totals_results.keys()):
            st.subheader(league)
            stats = stats_cache.get(league, {})
            injuries = inj_cache.get(league, {})
            for line, picks in totals_results[league]:
                competitors = line.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})
                home_team = home.get("team", {}).get("displayName", "Home")
                away_team = away.get("team", {}).get("displayName", "Away")
                start = iso_to_local_text(line.get("date", ""))

                st.markdown(f"**{away_team} @ {home_team}** — {start}")
                for pick in picks:
                    st.markdown(f"- {pick}")

                # Attach quick stats
                home_stats = stats.get(_normalize_team(home_team), {})
                away_stats = stats.get(_normalize_team(away_team), {})
                stat_line = []
                if home_stats.get("off_pts") and home_stats.get("def_pts"):
                    stat_line.append(
                        f"{home_team} off/def: {home_stats.get('off_pts')}/{home_stats.get('def_pts')}"
                    )
                if away_stats.get("off_pts") and away_stats.get("def_pts"):
                    stat_line.append(
                        f"{away_team} off/def: {away_stats.get('off_pts')}/{away_stats.get('def_pts')}"
                    )
                if stat_line:
                    st.caption(" • ".join(stat_line))

                # Injuries snippet (game-level first, fallback to league-level)
                home_inj = game_inj.get(_normalize_team(home_team), []) or injuries.get(_normalize_team(home_team), [])
                away_inj = game_inj.get(_normalize_team(away_team), []) or injuries.get(_normalize_team(away_team), [])
                home_inj = home_inj[:3]
                away_inj = away_inj[:3]
                if home_inj or away_inj:
                    txt = []
                    if home_inj:
                        txt.append(
                            f"{home_team} injuries: " + "; ".join(f"{r['player']} ({r['status']})" for r in home_inj)
                        )
                    if away_inj:
                        txt.append(
                            f"{away_team} injuries: " + "; ".join(f"{r['player']} ({r['status']})" for r in away_inj)
                        )
                    st.caption(" | ".join(txt))

                # Simple recommendation tag
                rec = "Lean Over" if any(p.startswith("FOLLOW: Over") for p in picks) else "Lean Under"
                st.badge(rec)
                st.divider()
