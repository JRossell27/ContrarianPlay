import streamlit as st

from contrarian import (
    LEAGUE_ALLOWLIST,
    get_contrarian_picks,
    iso_to_local_text,
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

if fetch:
    with st.spinner("Fetching odds and crunching moves..."):
        try:
            results = get_contrarian_picks(
                leagues=leagues,
                spread_threshold=spread_threshold,
                total_threshold=total_threshold,
                moneyline_threshold=moneyline_threshold,
            )
        except Exception as exc:
            st.error(f"Failed to fetch/process odds: {exc}")
            st.stop()

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
else:
    st.info("Set your thresholds and click “Find contrarian plays”.")
