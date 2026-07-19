"""Streamlit UI: paste or upload postcodes, get Yes/No per service category.

Run:  py -m streamlit run app.py
"""

import io

import pandas as pd
import streamlit as st

import checker

st.set_page_config(page_title="Service Proximity Checker", page_icon="🗺️", layout="wide")
st.title("Service Proximity Checker")
st.caption(
    "For each postcode: is a GP, hospital, secondary school, primary school and "
    "supermarket within the drive-time threshold?"
)


@st.cache_resource
def get_pois():
    return checker.load_pois()


try:
    pois = get_pois()
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()

with st.sidebar:
    st.header("Settings")
    threshold = st.slider("Drive-time threshold (minutes)", 5, 60, 25, 5)
    counts = {c: len(pois[c]) for c in checker.CATEGORIES}
    st.subheader("POI data loaded")
    for cat, n in counts.items():
        st.text(f"{checker.CATEGORY_LABELS[cat]}: {n:,}")

tab_paste, tab_upload = st.tabs(["Paste postcodes", "Upload CSV"])
postcodes = []

with tab_paste:
    text = st.text_area(
        "One postcode per line (commas also fine)",
        height=180,
        placeholder="SW1A 1AA\nM1 1AE\nYO62 4LB",
    )
    if text.strip():
        postcodes = [p.strip() for chunk in text.splitlines() for p in chunk.split(",") if p.strip()]

with tab_upload:
    up = st.file_uploader("CSV with postcodes in the first column", type=["csv"])
    if up is not None:
        updf = pd.read_csv(up, header=None, dtype=str)
        first = str(updf.iloc[0, 0]).lower()
        if "postcode" in first:  # header row
            updf = updf.iloc[1:]
        postcodes = [p for p in updf.iloc[:, 0].dropna().astype(str).str.strip() if p]

if postcodes:
    st.info(f"{len(postcodes)} postcode(s) ready.")

if st.button("Run check", type="primary", disabled=not postcodes):
    prog = st.progress(0.0, text="Starting...")

    def cb(done, pc):
        prog.progress(done / len(postcodes), text=f"Checked {done}/{len(postcodes)}: {pc}")

    df = checker.run_batch(postcodes, threshold_min=threshold, progress_cb=cb, pois=pois)
    prog.empty()

    n_ok = (df["all_within"] == "Yes").sum()
    st.subheader("Results")
    st.metric(f"All services within {threshold} min", f"{n_ok} / {len(df)}")

    def colour(v):
        # translucent tints read well on both light and dark themes
        if v == "Yes":
            return "background-color: rgba(33, 195, 84, 0.22)"
        if v == "No":
            return "background-color: rgba(255, 75, 75, 0.22)"
        return ""

    show_cols = ["postcode", "status", "all_within"] + [
        checker.CATEGORY_LABELS[c] for c in checker.CATEGORIES
    ]
    st.dataframe(
        df[show_cols].style.map(colour, subset=show_cols[2:]),
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Nearest site per category")
    nearest = pd.DataFrame({"postcode": df["postcode"]})
    for c in checker.CATEGORIES:
        label = checker.CATEGORY_LABELS[c]

        def fmt(r, label=label):
            name, mins = r[f"{label} nearest"], r[f"{label} (mins)"]
            if not name:
                return ""
            return f"{name} ({mins} min)" if pd.notna(mins) else name

        nearest[label] = df.apply(fmt, axis=1)
    st.dataframe(nearest, use_container_width=True, hide_index=True)

    with st.expander("Full data (all columns)"):
        st.dataframe(df, use_container_width=True, hide_index=True)

    buf = io.StringIO()
    df.to_csv(buf, index=False)
    st.download_button("Download results CSV", buf.getvalue(), "proximity_results.csv", "text/csv")

st.caption(
    "Data: GIAS (schools), NHS ODS (GPs), OpenStreetMap (hospitals, supermarkets), "
    "postcodes.io (geocoding), OSRM demo server (drive times, free-flow traffic). "
    "MVP - for production, self-host OSRM."
)
