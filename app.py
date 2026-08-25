from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from runner_watch.market_data import YahooMarketData
from runner_watch.models import ScanResult, ScanSettings
from runner_watch.sample_data import SAMPLE_SYMBOLS, SampleMarketData
from runner_watch.scanner import RunnerScanner
from runner_watch.universe import broad_us_universe, parse_custom_symbols, starter_universe

EASTERN = ZoneInfo("America/New_York")


st.set_page_config(
    page_title="Runner Watch",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .block-container {padding-top: 2rem; padding-bottom: 4rem;}
    [data-testid="stMetricValue"] {font-variant-numeric: tabular-nums;}
    .runner-note {border-left: 3px solid #4ADE80; padding: .75rem 1rem;
                  background: rgba(74, 222, 128, .07); border-radius: .35rem;}
    </style>
    """,
    unsafe_allow_html=True,
)


def _money(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:,.0f}"


def _result_frame(result: ScanResult) -> pd.DataFrame:
    rows = []
    for item in result.rows:
        rows.append(
            {
                "Ticker": item.ticker,
                "Score": item.score,
                "Stage": item.stage,
                "Price": item.price,
                "Change": item.change_pct,
                "5m": item.momentum_5m_pct,
                "15m": item.momentum_15m_pct,
                "Same-time RVOL": item.relative_volume,
                "Recent RVOL": item.recent_relative_volume,
                "Prior-high break": item.breakout_pct,
                "Dollar volume": item.dollar_volume,
                "Quote age": item.stale_minutes,
                "Signals": " · ".join(item.signals),
                "Risks": " · ".join(item.risks),
            }
        )
    return pd.DataFrame(rows)


def _progress_callback(widget: st.delta_generator.DeltaGenerator):
    def update(done: int, total: int, label: str) -> None:
        fraction = min(1.0, done / total) if total else 0.0
        widget.progress(fraction, text=label)

    return update


@st.cache_data(ttl=90, show_spinner=False)
def _live_chart(ticker: str) -> pd.DataFrame:
    result = YahooMarketData(batch_size=1).intraday([ticker])
    frame = result.frames.get(ticker, pd.DataFrame())
    if frame.empty:
        return frame
    current_date = frame.index[-1].date()
    return frame[frame.index.date == current_date][["Close", "Volume"]].copy()


now_et = datetime.now(UTC).astimezone(EASTERN)
heading, clock = st.columns([4, 1])
with heading:
    st.title("⚡ Runner Watch")
    st.caption("Find unusual price and volume movement before it becomes an obvious top gainer.")
with clock:
    st.metric("New York time", now_et.strftime("%I:%M %p"))
    st.caption(now_et.strftime("%a, %b %d"))

with st.sidebar:
    st.header("Scan setup")
    data_mode = st.radio(
        "Data",
        ["Live Yahoo data", "Sample data"],
        help="Start with sample data to test the app without internet or rate limits.",
    )

    if data_mode == "Live Yahoo data":
        universe_choice = st.selectbox(
            "Ticker list",
            ["Quick starter list", "Full US market", "My ticker list"],
        )
        custom_text = ""
        if universe_choice == "My ticker list":
            custom_text = st.text_area(
                "Symbols",
                value="ACHR, ASTS, IONQ, RKLB, SOUN",
                help="Use commas or spaces.",
            )
    else:
        universe_choice = "Sample"
        custom_text = ""
        st.info("Sample mode creates fake market data. It never shows real prices.")

    st.subheader("Daily filters")
    price_range = st.slider("Price range", 0.10, 200.0, (0.50, 50.0), step=0.10)
    min_avg_volume = st.select_slider(
        "Minimum average shares/day",
        options=[0, 50_000, 100_000, 250_000, 500_000, 1_000_000, 2_000_000],
        value=100_000,
        format_func=lambda value: f"{value:,}",
    )
    min_dollar_volume = st.select_slider(
        "Minimum average dollars/day",
        options=[0, 100_000, 250_000, 500_000, 1_000_000, 2_500_000, 5_000_000],
        value=500_000,
        format_func=_money,
    )
    if universe_choice == "Full US market":
        max_symbols = st.slider("Intraday scan cap", 100, 2000, 500, step=100)
        st.caption("A higher cap gives wider coverage but takes longer and may hit Yahoo limits.")
    else:
        max_symbols = 500
    top_n = st.slider("Results to keep", 10, 100, 50, step=10)
    scan_clicked = st.button("Run scan", type="primary", width="stretch")

if scan_clicked:
    universe_warnings: list[str] = []
    if data_mode == "Sample data":
        symbols = SAMPLE_SYMBOLS
        provider = SampleMarketData()
    else:
        provider = YahooMarketData(batch_size=75)
        if universe_choice == "Quick starter list":
            symbols = [item.symbol for item in starter_universe()]
        elif universe_choice == "Full US market":
            with st.spinner("Refreshing the official US ticker list…"):
                entries, universe_warnings = broad_us_universe()
            symbols = [item.symbol for item in entries]
        else:
            symbols = parse_custom_symbols(custom_text)

    settings = ScanSettings(
        min_price=price_range[0],
        max_price=price_range[1],
        min_avg_volume=min_avg_volume,
        min_avg_dollar_volume=min_dollar_volume,
        max_symbols=max_symbols,
        top_n=top_n,
    )
    progress_widget = st.progress(0, text="Starting scan…")
    try:
        result = RunnerScanner(provider).scan(
            symbols, settings, progress=_progress_callback(progress_widget)
        )
        result.warnings[:0] = universe_warnings
        st.session_state["scan_result"] = result
        st.session_state["scan_mode"] = data_mode
    except Exception as exc:
        st.error(f"The scan stopped: {exc}")
    finally:
        progress_widget.empty()

result: ScanResult | None = st.session_state.get("scan_result")
if result is None:
    st.markdown(
        """
        <div class="runner-note">
        <b>Start with Sample data.</b> It shows how the ranking works without waiting
        for a live download.
        Then switch to Yahoo data for a real scan.
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.subheader("What counts as an early runner?")
    cols = st.columns(4)
    cols[0].metric("1", "Unusual volume", help="More shares than this time on recent days")
    cols[1].metric("2", "Acceleration", help="Positive movement over 5 and 15 minutes")
    cols[2].metric("3", "Breakout", help="Price moving through the prior day's high")
    cols[3].metric("4", "Not extended", help="Moves above 15–20% get an extension warning")
    st.stop()

summary = st.columns(5)
summary[0].metric("Requested", f"{result.requested_symbols:,}")
summary[1].metric("Passed daily filter", f"{result.liquid_symbols:,}")
summary[2].metric("Checked intraday", f"{result.scanned_symbols:,}")
summary[3].metric("Matches shown", f"{len(result.rows):,}")
summary[4].metric("Scan time", f"{result.elapsed_seconds:.1f}s")

for warning in dict.fromkeys(result.warnings):
    if warning.startswith("Sample results"):
        st.info(warning)
    else:
        st.warning(warning)

frame = _result_frame(result)
if frame.empty:
    st.info("Nothing passed this scan. Try a wider price range or lower daily volume filters.")
    st.stop()

filter_col, score_col, export_col = st.columns([2, 1, 1])
with filter_col:
    stages = st.multiselect(
        "Show stages",
        ["EARLY", "BUILDING", "RUNNING", "WATCH", "EXTENDED"],
        default=["EARLY", "BUILDING", "RUNNING", "WATCH"],
    )
with score_col:
    minimum_score = st.slider("Minimum score", 0, 100, 20)
with export_col:
    st.caption("Export current results")
    st.download_button(
        "Download CSV",
        frame.to_csv(index=False).encode("utf-8"),
        file_name=f"runner-watch-{datetime.now().strftime('%Y%m%d-%H%M')}.csv",
        mime="text/csv",
        width="stretch",
    )

visible = frame[(frame["Stage"].isin(stages)) & (frame["Score"] >= minimum_score)]
st.subheader("Leaderboard")
st.dataframe(
    visible,
    hide_index=True,
    width="stretch",
    height=min(700, 88 + 35 * len(visible)),
    column_config={
        "Score": st.column_config.ProgressColumn(
            "Score", min_value=0, max_value=100, format="%.1f"
        ),
        "Price": st.column_config.NumberColumn("Price", format="$%.2f"),
        "Change": st.column_config.NumberColumn("Change", format="%+.1f%%"),
        "5m": st.column_config.NumberColumn("5m", format="%+.1f%%"),
        "15m": st.column_config.NumberColumn("15m", format="%+.1f%%"),
        "Same-time RVOL": st.column_config.NumberColumn("Same-time RVOL", format="%.1fx"),
        "Recent RVOL": st.column_config.NumberColumn("Recent RVOL", format="%.1fx"),
        "Prior-high break": st.column_config.NumberColumn("Prior-high break", format="%+.1f%%"),
        "Dollar volume": st.column_config.NumberColumn("Dollar volume", format="$%.0f"),
        "Quote age": st.column_config.NumberColumn("Quote age", format="%.0f min"),
    },
)

st.subheader("Ticker details")
selected_ticker = st.selectbox("Ticker", visible["Ticker"].tolist() or frame["Ticker"].tolist())
selected = next(item for item in result.rows if item.ticker == selected_ticker)
detail_cols = st.columns(5)
detail_cols[0].metric("Runner score", f"{selected.score:.1f}", selected.stage)
detail_cols[1].metric("Last price", f"${selected.price:.2f}", f"{selected.change_pct:+.1f}%")
detail_cols[2].metric(
    "Same-time volume",
    f"{selected.relative_volume:.1f}x" if selected.relative_volume is not None else "—",
)
detail_cols[3].metric("Session dollars", _money(selected.dollar_volume))
detail_cols[4].metric("Quote age", f"{selected.stale_minutes:.0f} min")

left, right = st.columns(2)
with left:
    st.markdown("**Signals**")
    if selected.signals:
        for signal in selected.signals:
            st.success(signal)
    else:
        st.caption("No strong signal tags yet.")
with right:
    st.markdown("**Risks**")
    if selected.risks:
        for risk in selected.risks:
            st.error(risk)
    else:
        st.caption("No automatic risk tags. This does not mean the trade is safe.")

if st.session_state.get("scan_mode") == "Live Yahoo data":
    with st.spinner("Loading the latest chart…"):
        chart = _live_chart(selected_ticker)
    if not chart.empty:
        st.line_chart(chart["Close"], height=280, width="stretch")

with st.expander("How the score works"):
    st.markdown(
        """
The 0–100 score combines same-time relative volume, recent 15-minute relative volume,
5- and 15-minute price movement, the prior-day-high breakout, session-high strength,
and traded dollar volume. Old quotes reduce the score. A move that is already very large
also loses points and gets an **EXTENDED** warning.

This is a discovery tool, not a buy signal. Free Yahoo data is unofficial and may be late,
missing, or wrong. Confirm the quote, spread, halt status, news, float, and liquidity in a
live broker before making a decision.
        """
    )
