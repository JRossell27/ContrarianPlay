# Contrarian Plays Finder

CLI that scrapes ESPN’s odds page and compares opening lines to current lines to surface contrarian angles on spreads, totals, and moneylines across major US leagues.

## Data source
`https://www.espn.com/sports-betting/odds` (no keys or accounts required). The page includes both openers and the latest prices.

## Install
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run (zero config)
```bash
python contrarian.py
```

Optional knobs:
```bash
python contrarian.py \
  --spread-threshold 1.0 \
  --total-threshold 1.5 \
  --moneyline-threshold 0.05
```

- Supported leagues: NBA, NHL, MLB, NFL, NCAAF, NCAAM (men’s college hoops).
- Thresholds: spreads/totals in points; moneyline in implied probability change (e.g., `0.05` = +5 percentage points).

## Streamlit UI
Run a simple web UI locally:
```bash
streamlit run streamlit_app.py
```
Use the controls to pick leagues and adjust thresholds; click “Find contrarian plays” to refresh.

## How contrarian picks are chosen
- **Spreads:** If the favorite is steamed (line gets more negative) by the threshold, back the dog with the extra points; if the dog is steamed, back the favorite at the better number.
- **Totals:** If the total rises enough, lean under; if it falls enough, lean over.
- **Moneylines:** If implied probability on one side rises by the threshold, back the other side (and vice versa).

## Notes and next steps
- The ESPN page is “top events,” so some lower-profile games may be missing; widen coverage by adding more league endpoints if needed.
- If you want public bet splits as a second filter, add a splits feed and require move + public % alignment before flagging plays.
