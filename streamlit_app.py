import streamlit as st

from contrarian import (
    LEAGUE_ALLOWLIST,
    collect_market_moves,
    confidence_label,
    extract_state,
    fetch_injuries,
    fetch_odds_page,
    iso_to_local_text,
    iter_events,
    _normalize_team,
)

import pandas as pd
import requests
from bs4 import BeautifulSoup
from functools import lru_cache
from typing import Optional, Dict, List


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
- ❌ If you can't explain the bet in one sentence ("Line moved ___ with no news, book protecting ___"), pass.
- 🔒🔒 **MULTI-SIGNAL LOCK** — spread AND moneyline both independently crossed their thresholds pointing to the same team. Highest confidence tier.
- 🔒 **LOCK (≥75%)** — large magnitude move, convergence bonus, vig confirmation, and/or key-number crossing. Very high confidence.
- ⚡ **STRONG (50–74%)** — meaningful move with partial confirmation.
- ✅ **LEAN (25–49%)** — threshold met but single signal only.
- 👁 **WATCH (<25%)** — borderline; treat as informational only.
- Score is *reduced* when spread and ML move in opposite directions (conflict penalty).
        """
    )


def _pick_icon(score: float) -> str:
    if score >= 0.75:
        return "🔒"
    if score >= 0.50:
        return "⚡"
    if score >= 0.25:
        return "✅"
    return "👁"


def _render_pick(pick_text: str, score: float) -> None:
    """Render a single pick line with appropriate icon, label, and highlight."""
    icon = _pick_icon(score)
    label = confidence_label(score)
    is_multi = pick_text.startswith("[MULTI-SIGNAL]")
    display_text = pick_text.replace("[MULTI-SIGNAL] ", "")
    if is_multi:
        st.markdown(
            f"- 🔒🔒 **[MULTI-SIGNAL LOCK {score*100:.0f}%]** {display_text}"
        )
    else:
        st.markdown(f"- {icon} **[{label} {score*100:.0f}%]** {display_text}")


@st.cache_data(ttl=300)
def cached_state():
    html = fetch_odds_page()
    return extract_state(html)


@st.cache_data(ttl=900)
def cached_injuries(league: str):
    return fetch_injuries(league)


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
            picks = collect_market_moves(
                league,
                line,
                spread_threshold=spread_threshold,
                total_threshold=total_threshold,
                moneyline_threshold=moneyline_threshold,
            )
            if picks:
                results.setdefault(league, []).append((line, picks, league_inj))

    if not results:
        st.info("No contrarian candidates found with the current thresholds.")
    else:
        for league in sorted(results.keys()):
            st.header(league)
            for line, picks, league_inj in results[league]:
                competitors = line.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})
                home_team = home.get("team", {}).get("displayName", "Home")
                away_team = away.get("team", {}).get("displayName", "Away")
                start = iso_to_local_text(line.get("date", ""))
                st.subheader(f"{away_team} @ {home_team}")
                st.caption(start)
                link = game_url(league, line.get("id", ""))
                if link:
                    st.caption(f"[ESPN game page]({link})")
                # Injury snippets
                home_inj = league_inj.get(_normalize_team(home_team), [])[:3]
                away_inj = league_inj.get(_normalize_team(away_team), [])[:3]
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
                # picks are sorted by score descending; display with confidence icon.
                for pick_text, score in picks:
                    _render_pick(pick_text, score)
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
    nba_thresh = cols[0].slider("NBA / NCAAM total move ≥ (pts)", 0.5, 6.0, defaults["NBA"], 0.5, key="totals_nba")
    nhl_thresh = cols[1].slider("NHL total move ≥ (pts)", 0.25, 3.0, defaults["NHL"], 0.25, key="totals_nhl")
    nfl_thresh = cols[2].slider("NFL / NCAAF total move ≥ (pts)", 0.5, 8.0, defaults["NFL"], 0.5, key="totals_nfl")
    mlb_thresh = st.slider("MLB total move ≥ (pts)", 0.5, 5.0, defaults["MLB"], 0.5, key="totals_mlb")

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
        picks = collect_market_moves(
            league,
            line,
            spread_threshold=1e6,  # disable spread
            total_threshold=sport_total_thresholds[league],
            moneyline_threshold=1.0,  # disable ML
        )
        total_picks = [
            (text, score)
            for text, score in picks
            if text.startswith("FOLLOW: Over") or text.startswith("FOLLOW: Under")
        ]
        if total_picks:
            totals_results.setdefault(league, []).append((line, total_picks, league_inj))

    if not totals_results:
        st.info("No totals moves met the sport-specific thresholds.")
    else:
        for league in sorted(totals_results.keys()):
            st.subheader(league)
            for line, picks, league_inj in totals_results[league]:
                competitors = line.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})
                home_team = home.get("team", {}).get("displayName", "Home")
                away_team = away.get("team", {}).get("displayName", "Away")
                start = iso_to_local_text(line.get("date", ""))
                st.markdown(f"**{away_team} @ {home_team}** — {start}")
                home_inj = league_inj.get(_normalize_team(home_team), [])[:3]
                away_inj = league_inj.get(_normalize_team(away_team), [])[:3]
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
                for pick_text, score in picks:
                    _render_pick(pick_text, score)
                st.divider()
else:
    st.info('Set your thresholds and click "Find contrarian plays".')

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
nba_thresh_ctx = cols[0].slider("NBA / NCAAM total move ≥ (pts)", 0.5, 6.0, defaults["NBA"], 0.5, key="ctx_nba")
nhl_thresh_ctx = cols[1].slider("NHL total move ≥ (pts)", 0.25, 3.0, defaults["NHL"], 0.25, key="ctx_nhl")
nfl_thresh_ctx = cols[2].slider("NFL / NCAAF total move ≥ (pts)", 0.5, 8.0, defaults["NFL"], 0.5, key="ctx_nfl")
mlb_thresh_ctx = st.slider("MLB total move ≥ (pts)", 0.5, 5.0, defaults["MLB"], 0.5, key="ctx_mlb")

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

    totals_results_ctx = {}
    sport_total_thresholds_ctx = {
        "NBA": nba_thresh_ctx,
        "NCAAM": nba_thresh_ctx,
        "NHL": nhl_thresh_ctx,
        "NFL": nfl_thresh_ctx,
        "NCAAF": nfl_thresh_ctx,
        "MLB": mlb_thresh_ctx,
    }

    for league, line in iter_events(state):
        if league not in sport_total_thresholds_ctx:
            continue
        league_inj = inj_cache.get(league, {})
        picks = collect_market_moves(
            league,
            line,
            spread_threshold=1e6,  # disable spread
            total_threshold=sport_total_thresholds_ctx[league],
            moneyline_threshold=1.0,  # disable ML
        )
        total_picks = [
            (text, score)
            for text, score in picks
            if text.startswith("FOLLOW: Over") or text.startswith("FOLLOW: Under")
        ]
        if total_picks:
            totals_results_ctx.setdefault(league, []).append((line, total_picks, league_inj))

    if not totals_results_ctx:
        st.info("No totals moves met the sport-specific thresholds.")
    else:
        for league in sorted(totals_results_ctx.keys()):
            st.subheader(league)
            stats = stats_cache.get(league, {})
            injuries = inj_cache.get(league, {})
            for line, picks, league_inj in totals_results_ctx[league]:
                competitors = line.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})
                home_team = home.get("team", {}).get("displayName", "Home")
                away_team = away.get("team", {}).get("displayName", "Away")
                start = iso_to_local_text(line.get("date", ""))

                st.markdown(f"**{away_team} @ {home_team}** — {start}")
                for pick_text, score in picks:
                    _render_pick(pick_text, score)

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

                # Injuries snippet (league-level only)
                home_inj = injuries.get(_normalize_team(home_team), [])[:3]
                away_inj = injuries.get(_normalize_team(away_team), [])[:3]
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

                # Simple recommendation tag based on highest-confidence pick.
                best_pick_text = picks[0][0]  # already sorted by score desc
                rec = "Lean Over" if best_pick_text.startswith("FOLLOW: Over") else "Lean Under"
                st.badge(rec)
                st.divider()
