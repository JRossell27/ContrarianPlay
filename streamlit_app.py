import streamlit as st

from contrarian import (
    LEAGUE_ALLOWLIST,
    collect_contrarian_picks,
    extract_state,
    fetch_odds_page,
    iso_to_local_text,
    iter_events,
)


st.set_page_config(page_title="Contrarian Plays", page_icon="📉", layout="wide")

st.title("📉 Contrarian Plays Finder")
st.caption(
    "Scrapes ESPN odds (open vs current) and highlights contrarian angles on spreads, totals, and moneylines."
)

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
            picks = collect_contrarian_picks(
                line,
                spread_threshold=spread_threshold,
                total_threshold=total_threshold,
                moneyline_threshold=moneyline_threshold,
            )
            if picks:
                results.setdefault(league, []).append((line, picks))

    if not results:
        st.info("No contrarian candidates found with the current thresholds.")
    else:
        for league in sorted(results.keys()):
            st.header(league)
            for line, picks in results[league]:
                competitors = line.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})
                home_team = home.get("team", {}).get("displayName", "Home")
                away_team = away.get("team", {}).get("displayName", "Away")
                start = iso_to_local_text(line.get("date", ""))
                st.subheader(f"{away_team} @ {home_team}")
                st.caption(start)
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
        picks = collect_contrarian_picks(
            line,
            spread_threshold=1e6,  # disable spread
            total_threshold=sport_total_thresholds[league],
            moneyline_threshold=1.0,  # disable ML
        )
        total_picks = [p for p in picks if p.startswith("Bet: Over") or p.startswith("Bet: Under")]
        if total_picks:
            totals_results.setdefault(league, []).append((line, total_picks))

    if not totals_results:
        st.info("No totals moves met the sport-specific thresholds.")
    else:
        for league in sorted(totals_results.keys()):
            st.subheader(league)
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
                st.divider()
else:
    st.info("Set your thresholds and click “Find contrarian plays”.")
