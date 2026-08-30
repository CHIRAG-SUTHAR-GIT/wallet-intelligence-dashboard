"""Wallet Intelligence & Transaction Correlation System.

Correlates one "master" Binance report against any number of "sub"
reports, finds the counterparties they share, and reports the value
that actually moved between them.
"""

import io
import re
from collections import defaultdict
from datetime import timedelta

import altair as alt
import networkx as nx
import numpy as np
import pandas as pd
import streamlit as st

# =========================================================
# PAGE CONFIG
# =========================================================

st.set_page_config(
    page_title="Wallet Intelligence Dashboard",
    layout="wide"
)

# =========================================================
# SHEET PROFILES
# =========================================================
#
# Every profile resolves its columns by alias, so a report that
# renames "Address " to "Deposit Address" still works. Direction says
# how to turn a magnitude into signed value:
#
#   out    -> always an outflow  (negative)
#   in     -> always an inflow   (positive)
#   signed -> the sheet already carries the sign
#
# The user can override any resolved column in the sidebar, which is
# what makes the sheets without a profile usable too.

PROFILES = {

    "Withdrawal History": {
        "sheet": ["withdrawal history"],
        "key": ["destination address", "address", "to address"],
        "amount": ["usdt", "usdt equivalent", "amount"],
        "txid": ["txid", "transaction id", "tx hash"],
        "time": ["apply time", "create time", "date", "time"],
        "status": ["status"],
        "currency": ["currency", "asset", "coin"],
        "network": ["network", "chain"],
        "direction": "out",
        "settled": {"success", "completed", "successful"},
    },

    "Deposit History": {
        "sheet": ["deposit history"],
        "key": ["deposit address", "address", "source address"],
        "amount": ["usdt", "usdt equivalent", "amount"],
        "txid": ["txid", "transaction id", "tx hash"],
        "time": ["create time", "date", "time"],
        "status": ["status"],
        "currency": ["currency", "asset", "coin"],
        "network": ["network", "chain"],
        "direction": "in",
        "settled": {"success", "completed", "successful"},
    },

    "Binance Pay": {
        "sheet": ["binance pay", "binance pay history", "pay history"],
        "key": ["counterparty binance id", "counterparty wallet id",
                "merchant name"],
        "amount": ["usdt equivalent", "usdt", "amount"],
        "txid": ["transaction id", "order id"],
        "time": ["transaction time", "create time", "time"],
        "status": ["status"],
        "currency": ["currency", "asset", "coin"],
        "network": ["network", "chain"],
        "direction": "signed",
        "settled": set(),
    },

    "Attempted Withdrawal History": {
        "sheet": ["attempted withdrawal history"],
        "key": ["address", "destination address"],
        "amount": ["usdt equivalent", "usdt", "amount"],
        "txid": ["txid", "transaction id"],
        "time": ["date", "create time", "time"],
        "status": ["decisioncode", "status"],
        "currency": ["asset", "currency", "coin"],
        "network": ["network", "chain"],
        "direction": "out",
        "settled": set(),
    },

    "P2P": {
        "sheet": ["p2p"],
        "key": ["ad publisher id", "take id", "target uid"],
        "amount": ["usdt equivalent", "total amount", "amount"],
        "txid": ["order id", "ad number"],
        "time": ["create time", "release time", "payment time"],
        "status": ["status"],
        "currency": ["crypto", "currency", "asset"],
        "network": ["network", "chain"],
        "direction": "signed",
        "settled": {"completed"},
    },
}

# Combined modes read several sheets from every file at once. This is
# what lets a withdrawal in one report be matched to the deposit that
# same transfer created in another -- they never share a sheet.

COMBOS = {
    "Deposits + Withdrawals (combined)": [
        "Withdrawal History", "Deposit History",
    ],
}

CUSTOM = "Custom (pick any sheet)"

# Round-number tiers used by the structuring heuristic.
ROUND_STEPS = [100, 500, 1000, 5000, 10000]

# =========================================================
# COLUMN / KEY HELPERS
# =========================================================


def norm(text):
    """Collapse whitespace and case so headers compare reliably."""

    return re.sub(r"\s+", " ", str(text)).strip().casefold()


def resolve_column(df, aliases):
    """Find the real column name matching any alias, else None.

    Tries exact normalised match first, then a substring match, so
    "Destination Address " and "Withdrawal Destination Address" both
    resolve to the same alias.
    """

    lookup = {norm(c): c for c in df.columns}

    for alias in aliases:
        if alias in lookup:
            return lookup[alias]

    for alias in aliases:
        for normalised, actual in lookup.items():
            if alias in normalised:
                return actual

    return None


def resolve_sheet(sheet_names, aliases):
    """Find the real sheet name matching any alias, else None."""

    lookup = {norm(s): s for s in sheet_names}

    for alias in aliases:
        if alias in lookup:
            return lookup[alias]

    for alias in aliases:
        for normalised, actual in lookup.items():
            if alias in normalised:
                return actual

    return None


def normalize_key(series):
    """Trim a match column to a clean string key.

    Binance ID columns are numeric, so pandas reads them back as
    floats when any row is blank ("200000002.0"). Addresses never end
    in ".0", so stripping that suffix is safe on every sheet.
    """

    cleaned = (
        series
        .astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )

    # Excel leaves literal blanks and pandas leaves the string "nan";
    # neither is a counterparty.
    return cleaned.mask(
        cleaned.str.casefold().isin({"", "nan", "none", "-", "n/a"})
    )


def short(value, head=10, tail=6):
    """Shorten a long address for display without losing identity."""

    text = str(value)

    if len(text) <= head + tail + 3:
        return text

    return f"{text[:head]}...{text[-tail:]}"


# =========================================================
# CACHED FILE READING
# =========================================================


@st.cache_data(show_spinner=False)
def list_sheets(payload):
    """Sheet names of a workbook, cached on its bytes."""

    with pd.ExcelFile(io.BytesIO(payload)) as book:
        return list(book.sheet_names)


@st.cache_data(show_spinner=False)
def read_sheet(payload, sheet_name):
    """One sheet of a workbook, cached on its bytes plus sheet."""

    return pd.read_excel(io.BytesIO(payload), sheet_name=sheet_name)


@st.cache_data(show_spinner=False)
def read_identity(payload):
    """Pull UID and email out of the Customer Information banner.

    Binance writes the account a report belongs to into that sheet's
    first header cell, e.g.
    "User Basic Information(id: 100000001  email: a@b.com time: ...)".
    That lets every report label itself instead of relying on a
    filename nobody can read.
    """

    try:
        sheets = list_sheets(payload)
    except Exception:
        return {}

    sheet = resolve_sheet(sheets, ["customer information", "customer info"])

    if sheet is None:
        return {}

    try:
        banner = pd.read_excel(
            io.BytesIO(payload),
            sheet_name=sheet,
            header=None,
            nrows=4
        )
    except Exception:
        return {}

    blob = " ".join(str(v) for v in banner.to_numpy().ravel())

    uid = re.search(r"id:\s*(\d+)", blob)
    email = re.search(r"email:\s*([^\s,;]+@[^\s,;]+)", blob)

    identity = {}

    if uid:
        identity["uid"] = uid.group(1)

    if email:
        identity["email"] = email.group(1)

    return identity


def label_for(file_name, identity):
    """Human-readable name for a report: email (uid), else filename."""

    email = identity.get("email")
    uid = identity.get("uid")

    if email and uid:
        return f"{email} ({uid})"

    if email:
        return email

    if uid:
        return f"UID {uid}"

    return re.sub(r"\.xlsx?$", "", file_name, flags=re.I)


# =========================================================
# EXTRACTION
# =========================================================


def extract(df, spec, role, file_name, label, uid):
    """Reduce one raw sheet to the unified long format.

    Everything downstream reads only these columns, so each sheet
    layout is dealt with exactly once, here.
    """

    key_col = spec["key_col"]
    amount_col = spec["amount_col"]

    out = pd.DataFrame(index=df.index)

    out["Key"] = normalize_key(df[key_col])

    raw_amount = pd.to_numeric(df[amount_col], errors="coerce")

    direction = spec["direction"]

    if direction == "out":
        signed = -raw_amount.abs()
    elif direction == "in":
        signed = raw_amount.abs()
    else:
        signed = raw_amount

    out["Signed USDT"] = signed
    out["Abs USDT"] = signed.abs()

    out["Flow"] = np.where(
        signed < 0, "Outflow",
        np.where(signed > 0, "Inflow", "Zero")
    )

    for name, col in [
        ("TXID", spec.get("txid_col")),
        ("Currency", spec.get("currency_col")),
        ("Network", spec.get("network_col")),
        ("Status", spec.get("status_col")),
    ]:
        if col and col in df.columns:
            out[name] = df[col].astype(str).str.strip()
        else:
            out[name] = pd.NA

    time_col = spec.get("time_col")

    if time_col and time_col in df.columns:
        out["Timestamp"] = pd.to_datetime(df[time_col], errors="coerce")
    else:
        out["Timestamp"] = pd.NaT

    settled = spec.get("settled") or set()

    if settled:
        out["Settled"] = (
            out["Status"].astype(str).str.strip().str.casefold().isin(settled)
        )
    else:
        out["Settled"] = True

    out["Role"] = role
    out["Source File"] = file_name
    out["Account"] = label
    out["UID"] = uid or ""

    # Original row number, so a finding can be traced back to the sheet.
    out["Row"] = df.index + 2

    return out.dropna(subset=["Key"])


def build_spec(df, profile, overrides):
    """Resolve every column a profile needs, honouring user overrides."""

    spec = {"direction": profile["direction"],
            "settled": profile.get("settled", set())}

    for field in ["key", "amount", "txid", "time", "status",
                  "currency", "network"]:

        chosen = overrides.get(field)

        if chosen and chosen in df.columns:
            spec[f"{field}_col"] = chosen
        else:
            spec[f"{field}_col"] = resolve_column(df, profile[field])

    if overrides.get("direction"):
        spec["direction"] = overrides["direction"]

    return spec


# =========================================================
# DEDUPLICATION
# =========================================================


def deduplicate(pool):
    """Collapse the same transfer seen from both sides of a trade.

    A withdrawal in the master and the matching deposit in a sub file
    are one movement of money recorded twice. Summing both inflates
    every total, so we keep one row and record the pairing -- which is
    itself the strongest evidence that two accounts are linked.
    """

    pool = pool.copy()
    pool["Duplicate"] = False

    has_tx = pool["TXID"].notna() & (
        pool["TXID"].astype(str).str.strip().str.len() > 6
    )

    keyed = pool[has_tx].copy()

    if keyed.empty:
        return pool, pd.DataFrame()

    keyed["TX Key"] = keyed["TXID"].astype(str).str.strip().str.casefold()

    counts = keyed.groupby("TX Key")["Source File"].nunique()

    shared = set(counts[counts > 1].index)

    if not shared:
        return pool, pd.DataFrame()

    pairs = []

    for tx_key in shared:
        rows = keyed[keyed["TX Key"] == tx_key]

        pairs.append({
            "TXID": rows["TXID"].iloc[0],
            "Accounts": " <-> ".join(sorted(rows["Account"].unique())),
            "Counterparty": rows["Key"].iloc[0],
            "Abs USDT": round(float(rows["Abs USDT"].max()), 6),
            "Copies": len(rows),
            "Timestamp": rows["Timestamp"].min(),
        })

    # Mark every copy after the first within a shared TXID.
    dupes = keyed[keyed["TX Key"].isin(shared)]

    dup_flags = dupes.groupby("TX Key").cumcount().gt(0)

    pool.loc[dup_flags.index, "Duplicate"] = dup_flags.values

    pairs_df = pd.DataFrame(pairs).sort_values("Abs USDT", ascending=False)

    return pool, pairs_df


def apply_counting(pool, drop_duplicates):
    """Decide which rows contribute value, without deleting any.

    A duplicate row is still evidence that two accounts touched the
    same counterparty -- deleting it would destroy the very link the
    duplicate proves. So the row stays and only its value is zeroed.
    """

    pool = pool.copy()

    if "Duplicate" not in pool.columns:
        pool["Duplicate"] = False

    pool["Counted"] = ~pool["Duplicate"] if drop_duplicates else True

    pool["Value USDT"] = pool["Signed USDT"].where(pool["Counted"], 0.0)
    pool["Volume USDT"] = pool["Abs USDT"].where(pool["Counted"], 0.0)

    return pool


def ensure_counted(pool):
    """Let analysis run on a frame that never went through counting."""

    if "Counted" in pool.columns:
        return pool

    return apply_counting(pool, drop_duplicates=True)


# =========================================================
# ANALYSIS
# =========================================================


def wallet_summary(pool):
    """Per-counterparty gross in, gross out, net and reach.

    Value comes only from counted rows, so a transfer recorded twice
    is not double-counted. Reach -- which accounts touched this
    counterparty -- comes from every row, duplicates included.
    """

    if pool.empty:
        return pd.DataFrame()

    pool = ensure_counted(pool)

    counted = pool[pool["Counted"]]

    inflow = (
        counted[counted["Value USDT"] > 0]
        .groupby("Key")["Value USDT"].sum()
    )

    outflow = (
        counted[counted["Value USDT"] < 0]
        .groupby("Key")["Value USDT"].sum().abs()
    )

    grouped = pool.groupby("Key")

    summary = pd.DataFrame({
        "Gross In USDT": inflow,
        "Gross Out USDT": outflow,
        "Tx Count": counted.groupby("Key").size(),
        "Rows Seen": grouped.size(),
        "Accounts": grouped["Account"].nunique(),
        "First Seen": grouped["Timestamp"].min(),
        "Last Seen": grouped["Timestamp"].max(),
        "Seen In": grouped["Account"].apply(
            lambda s: ", ".join(sorted(set(s)))
        ),
    })

    summary["Gross In USDT"] = summary["Gross In USDT"].fillna(0.0)
    summary["Gross Out USDT"] = summary["Gross Out USDT"].fillna(0.0)
    summary["Tx Count"] = summary["Tx Count"].fillna(0).astype(int)

    summary["Net USDT"] = summary["Gross In USDT"] - summary["Gross Out USDT"]

    summary["Total Volume USDT"] = (
        summary["Gross In USDT"] + summary["Gross Out USDT"]
    )

    summary = summary.reset_index().rename(columns={"Key": "Counterparty"})

    for col in ["Gross In USDT", "Gross Out USDT", "Net USDT",
                "Total Volume USDT"]:
        summary[col] = summary[col].round(6)

    ordered = ["Counterparty", "Gross In USDT", "Gross Out USDT", "Net USDT",
               "Total Volume USDT", "Tx Count", "Rows Seen", "Accounts",
               "First Seen", "Last Seen", "Seen In"]

    return summary[ordered].sort_values("Total Volume USDT", ascending=False)


def flow_table(pool):
    """Directional value between the master account and each sub."""

    pool = ensure_counted(pool)

    rows = []

    master_keys = set(pool.loc[pool["Role"] == "Master", "Key"])

    for account, chunk in pool[pool["Role"] == "Sub"].groupby("Account"):

        shared = master_keys & set(chunk["Key"])

        if not shared:
            continue

        master_side = pool[
            (pool["Role"] == "Master") & (pool["Key"].isin(shared))
        ]

        sub_side = chunk[chunk["Key"].isin(shared)]

        rows.append({
            "Sub Account": account,
            "Shared Counterparties": len(shared),
            "Master Out USDT": round(-float(
                master_side.loc[master_side["Value USDT"] < 0,
                                "Value USDT"].sum()
            ), 2),
            "Master In USDT": round(float(
                master_side.loc[master_side["Value USDT"] > 0,
                                "Value USDT"].sum()
            ), 2),
            "Sub Out USDT": round(-float(
                sub_side.loc[sub_side["Value USDT"] < 0,
                             "Value USDT"].sum()
            ), 2),
            "Sub In USDT": round(float(
                sub_side.loc[sub_side["Value USDT"] > 0,
                             "Value USDT"].sum()
            ), 2),
            "Master Tx": len(master_side),
            "Sub Tx": len(sub_side),
        })

    table = pd.DataFrame(rows)

    if table.empty:
        return table

    table["Total Volume USDT"] = (
        table["Master Out USDT"] + table["Master In USDT"]
        + table["Sub Out USDT"] + table["Sub In USDT"]
    ).round(2)

    return table.sort_values("Total Volume USDT", ascending=False)


def build_graph(pool):
    """Bipartite account<->counterparty graph, plus its clusters.

    This is what answers "which accounts are connected to each other",
    including sub-to-sub links the master never touches.
    """

    pool = ensure_counted(pool)

    graph = nx.Graph()

    for account, chunk in pool.groupby("Account"):
        graph.add_node(("account", account), kind="account")

        for key, rows in chunk.groupby("Key"):
            graph.add_node(("key", key), kind="key")

            graph.add_edge(
                ("account", account),
                ("key", key),
                volume=float(rows["Volume USDT"].sum()),
                count=len(rows),
            )

    clusters = []

    for component in nx.connected_components(graph):

        accounts = sorted(n[1] for n in component if n[0] == "account")
        keys = sorted(n[1] for n in component if n[0] == "key")

        if len(accounts) < 2:
            continue

        volume = sum(
            data["volume"]
            for _, _, data in graph.subgraph(component).edges(data=True)
        )

        clusters.append({
            "Accounts": len(accounts),
            "Counterparties": len(keys),
            "Total Volume USDT": round(volume, 2),
            "Members": ", ".join(accounts),
            "Sample Counterparties": ", ".join(short(k) for k in keys[:5]),
        })

    clusters_df = pd.DataFrame(clusters)

    if not clusters_df.empty:
        clusters_df = clusters_df.sort_values(
            "Total Volume USDT", ascending=False
        ).reset_index(drop=True)

        clusters_df.insert(0, "Cluster", range(1, len(clusters_df) + 1))

    return graph, clusters_df


def account_links(pool):
    """Account-to-account edges implied by a shared counterparty."""

    pool = ensure_counted(pool)

    edges = defaultdict(lambda: {"keys": set(), "volume": 0.0})

    for key, chunk in pool.groupby("Key"):

        accounts = sorted(set(chunk["Account"]))

        if len(accounts) < 2:
            continue

        volume = float(chunk["Volume USDT"].sum())

        for i, left in enumerate(accounts):
            for right in accounts[i + 1:]:
                edge = edges[(left, right)]
                edge["keys"].add(key)
                edge["volume"] += volume

    rows = [
        {
            "Account A": left,
            "Account B": right,
            "Shared Counterparties": len(data["keys"]),
            "Combined Volume USDT": round(data["volume"], 2),
            "Sample": ", ".join(short(k) for k in sorted(data["keys"])[:3]),
        }
        for (left, right), data in edges.items()
    ]

    table = pd.DataFrame(rows)

    if not table.empty:
        table = table.sort_values(
            ["Shared Counterparties", "Combined Volume USDT"],
            ascending=False
        )

    return table


def find_patterns(pool, window_hours, round_tolerance):
    """Structuring, repetition and co-occurrence heuristics."""

    findings = {}

    # Heuristics describe real transfers, so a duplicate copy of one
    # must not look like a repeated payment.
    pool = ensure_counted(pool)
    pool = pool[pool["Counted"]]

    if pool.empty:
        empty = pd.DataFrame()
        return {"repeats": empty, "near_round": empty, "cooccurrence": empty}

    # -- Repeated identical amounts ---------------------------------
    repeats = (
        pool.groupby(["Key", "Abs USDT"])
        .agg(Count=("Abs USDT", "size"),
             Accounts=("Account", lambda s: ", ".join(sorted(set(s)))))
        .reset_index()
    )

    repeats = repeats[(repeats["Count"] >= 3) & (repeats["Abs USDT"] > 0)]

    findings["repeats"] = repeats.sort_values(
        "Count", ascending=False
    ).rename(columns={"Key": "Counterparty", "Abs USDT": "Amount USDT"})

    # -- Just-under-a-round-number amounts --------------------------
    amounts = pool["Abs USDT"].dropna()

    hits = pd.Series(False, index=amounts.index)
    ceilings = pd.Series(np.nan, index=amounts.index)

    for step in ROUND_STEPS:
        gap = (step - (amounts % step)) % step

        hit = (gap > 0) & (gap <= step * round_tolerance) & (amounts >= step)

        ceilings = ceilings.mask(hit & ~hits,
                                 np.ceil(amounts / step) * step)
        hits = hits | hit

    near_df = pd.DataFrame({
        "Counterparty": pool.loc[hits[hits].index, "Key"],
        "Account": pool.loc[hits[hits].index, "Account"],
        "Amount USDT": pool.loc[hits[hits].index, "Abs USDT"].round(2),
        "Just Under": ceilings[hits[hits].index],
        "Timestamp": pool.loc[hits[hits].index, "Timestamp"],
    })

    if not near_df.empty:
        near_df = near_df.sort_values("Amount USDT", ascending=False)

    findings["near_round"] = near_df.reset_index(drop=True)

    # -- Two accounts hitting one counterparty close in time --------
    timed = pool.dropna(subset=["Timestamp"])

    co_rows = []

    for key, chunk in timed.groupby("Key"):

        if chunk["Account"].nunique() < 2:
            continue

        chunk = chunk.sort_values("Timestamp")

        stamps = chunk["Timestamp"].tolist()
        accounts = chunk["Account"].tolist()
        values = chunk["Abs USDT"].tolist()

        for i in range(len(stamps) - 1):
            for j in range(i + 1, len(stamps)):

                delta = stamps[j] - stamps[i]

                if delta > timedelta(hours=window_hours):
                    break

                if accounts[i] == accounts[j]:
                    continue

                co_rows.append({
                    "Counterparty": key,
                    "Account A": accounts[i],
                    "Account B": accounts[j],
                    "Gap Hours": round(delta.total_seconds() / 3600, 2),
                    "Amount A USDT": round(float(values[i]), 2),
                    "Amount B USDT": round(float(values[j]), 2),
                    "First Timestamp": stamps[i],
                })

    co_df = pd.DataFrame(co_rows)

    if not co_df.empty:
        co_df = co_df.sort_values("Gap Hours").reset_index(drop=True)

    findings["cooccurrence"] = co_df

    return findings


def concentration(summary):
    """Share of total volume carried by the top counterparties."""

    if summary.empty:
        return pd.DataFrame()

    total = summary["Total Volume USDT"].sum()

    if total <= 0:
        return pd.DataFrame()

    rows = []

    for n in [1, 3, 5, 10, 25]:
        if n > len(summary):
            break

        top = summary["Total Volume USDT"].head(n).sum()

        rows.append({
            "Top N": n,
            "Volume USDT": round(float(top), 2),
            "Share of Total": f"{top / total * 100:.1f}%",
        })

    return pd.DataFrame(rows)


# =========================================================
# EXPORT
# =========================================================

MONEY_COLS = {
    "Gross In USDT", "Gross Out USDT", "Net USDT", "Total Volume USDT",
    "Abs USDT", "Signed USDT", "Amount USDT", "Master Out USDT",
    "Master In USDT", "Sub Out USDT", "Sub In USDT", "Volume USDT",
    "Combined Volume USDT", "Amount A USDT", "Amount B USDT",
}

HEAT_COLS = {"Total Volume USDT", "Net USDT", "Combined Volume USDT"}


def write_workbook(sheets):
    """Write formatted sheets to an in-memory workbook.

    Sheets get a sequential prefix so two long filenames can never
    truncate onto the same 31-character sheet name.
    """

    buffer = io.BytesIO()

    with pd.ExcelWriter(buffer, engine="xlsxwriter",
                        datetime_format="yyyy-mm-dd hh:mm:ss") as writer:

        book = writer.book

        header_fmt = book.add_format({
            "bold": True, "bg_color": "#1F3864", "font_color": "white",
            "border": 1, "align": "left", "valign": "vcenter",
        })

        money_fmt = book.add_format({"num_format": "#,##0.00"})

        for position, (name, frame) in enumerate(sheets.items(), start=1):

            sheet_name = f"{position:02d}_{name}"[:31]

            if not isinstance(frame, pd.DataFrame) or frame.empty:
                frame = pd.DataFrame({"Note": ["No rows for this section."]})

            frame = frame.copy()

            # Excel cannot store timezone-aware datetimes.
            for col in frame.columns:
                if isinstance(frame[col].dtype, pd.DatetimeTZDtype):
                    frame[col] = frame[col].dt.tz_localize(None)

            frame.to_excel(writer, sheet_name=sheet_name, index=False,
                           startrow=1, header=False)

            sheet = writer.sheets[sheet_name]

            for col_index, col_name in enumerate(frame.columns):

                sheet.write(0, col_index, str(col_name), header_fmt)

                sample = frame[col_name].astype(str).str.len().head(200)

                width = max(len(str(col_name)) + 2,
                            int(sample.max() or 10) + 2)

                sheet.set_column(
                    col_index, col_index, min(width, 46),
                    money_fmt if col_name in MONEY_COLS else None
                )

                if col_name in HEAT_COLS and len(frame):
                    sheet.conditional_format(
                        1, col_index, len(frame), col_index,
                        {"type": "3_color_scale"}
                    )

            sheet.freeze_panes(1, 0)

            if len(frame):
                sheet.autofilter(0, 0, len(frame), len(frame.columns) - 1)

    buffer.seek(0)

    return buffer


# =========================================================
# HEADER
# =========================================================

st.title("Wallet Intelligence & Transaction Analysis")

st.caption(
    "Correlates one master report against any number of sub reports, "
    "de-duplicates transfers seen from both sides, and reports "
    "directional value."
)

st.markdown("---")

# =========================================================
# INPUT
# =========================================================

analysis_type = st.radio(
    "Select Analysis Type",
    list(PROFILES) + list(COMBOS) + [CUSTOM],
)

left, right = st.columns(2)

with left:
    master_file = st.file_uploader(
        "Upload Master Excel File",
        type=["xlsx", "xls"],
    )

with right:
    sub_files = st.file_uploader(
        "Upload Multiple Sub Excel Files",
        type=["xlsx", "xls"],
        accept_multiple_files=True,
    )

if not (master_file and sub_files):
    st.info("Upload a master report and at least one sub report to begin.")
    st.markdown("---")
    st.caption("Crypto Wallet Intelligence & Transaction Correlation System")
    st.stop()

master_bytes = master_file.getvalue()

# =========================================================
# SHEET + COLUMN RESOLUTION
# =========================================================

try:
    master_sheets = list_sheets(master_bytes)
except Exception as exc:
    st.error(f"Could not open the master file: {exc}")
    st.stop()

st.sidebar.header("Sheet & columns")

combined = analysis_type in COMBOS

if combined:

    # Combined mode spans several sheets, so a single set of column
    # pickers would be meaningless. Columns resolve by alias instead.
    members = [(name, PROFILES[name]) for name in COMBOS[analysis_type]]

    sheet_name = None
    overrides = {}

    st.sidebar.info(
        "Reading " + " and ".join(n for n, _ in members)
        + " from every file, resolving columns automatically."
    )

else:

    if analysis_type == CUSTOM:
        profile = {
            "sheet": [], "key": [], "amount": [], "txid": [], "time": [],
            "status": [], "currency": [], "network": [],
            "direction": "signed", "settled": set(),
        }
        default_sheet = None
    else:
        profile = PROFILES[analysis_type]
        default_sheet = resolve_sheet(master_sheets, profile["sheet"])

    if default_sheet is None:

        if analysis_type != CUSTOM:
            st.sidebar.warning(
                f'No sheet matching "{analysis_type}" in the master file. '
                "Pick one manually."
            )

        default_sheet = master_sheets[0]

    sheet_name = st.sidebar.selectbox(
        "Sheet",
        master_sheets,
        index=master_sheets.index(default_sheet),
    )

    try:
        master_raw = read_sheet(master_bytes, sheet_name)
    except Exception as exc:
        st.error(f'Could not read sheet "{sheet_name}": {exc}')
        st.stop()

    columns = [str(c) for c in master_raw.columns]

    def picker(field, caption, allow_none=True):
        """Sidebar override for one resolved column."""

        guess = resolve_column(master_raw, profile[field])

        options = (["(none)"] if allow_none else []) + columns

        index = options.index(str(guess)) if str(guess) in options else 0

        choice = st.sidebar.selectbox(caption, options, index=index)

        if choice == "(none)":
            return None

        # Map the display string back to the real column object.
        return master_raw.columns[columns.index(choice)]

    overrides = {
        "key": picker("key", "Match on column", allow_none=False),
        "amount": picker("amount", "Amount column", allow_none=False),
        "time": picker("time", "Timestamp column"),
        "txid": picker("txid", "Transaction ID column"),
        "status": picker("status", "Status column"),
        "currency": picker("currency", "Currency column"),
        "network": picker("network", "Network column"),
    }

    direction_labels = {
        "out": "Always outflow (money leaving)",
        "in": "Always inflow (money arriving)",
        "signed": "Sign is already in the amount column",
    }

    direction_keys = list(direction_labels)

    overrides["direction"] = st.sidebar.selectbox(
        "Direction of value",
        direction_keys,
        index=direction_keys.index(profile["direction"]),
        format_func=lambda k: direction_labels[k],
    )

    members = [(analysis_type, profile)]

st.sidebar.markdown("---")
st.sidebar.header("Filters")

drop_duplicates = st.sidebar.checkbox(
    "De-duplicate transfers seen in two files", value=True,
    help="One transfer recorded by both sides is one movement of money, "
         "not two. Turn off to see raw per-file totals.",
)

settled_only = st.sidebar.checkbox(
    "Settled transactions only", value=True,
    help="Excludes failed, cancelled and rejected rows where the sheet "
         "has a status column.",
)

min_amount = st.sidebar.number_input(
    "Minimum amount (USDT)", min_value=0.0, value=0.0, step=100.0
)

window_hours = st.sidebar.slider(
    "Co-occurrence window (hours)", 1, 168, 24,
    help="Two accounts touching the same counterparty inside this window "
         "are reported as co-occurring.",
)

round_tolerance = st.sidebar.slider(
    "Round-number tolerance", 0.001, 0.10, 0.02,
    help="How close to a round number counts as 'just under'.",
)

# =========================================================
# LOAD EVERY FILE
# =========================================================

frames = []
issues = []
quality = []

file_plan = [("Master", master_file.name, master_bytes)] + [
    ("Sub", f.name, f.getvalue()) for f in sub_files
]

for role, file_name, payload in file_plan:

    # Per-file handling: one broken report must not kill the whole run.
    try:
        sheets = list_sheets(payload)

        identity = read_identity(payload)

        label = label_for(file_name, identity)

    except Exception as exc:
        issues.append(f"**{file_name}** - {type(exc).__name__}: {exc}")
        continue

    for member_name, member_profile in members:

        try:
            if combined:
                actual_sheet = resolve_sheet(sheets, member_profile["sheet"])
            else:
                actual_sheet = resolve_sheet(sheets, [norm(sheet_name)])

                if actual_sheet is None and analysis_type != CUSTOM:
                    actual_sheet = resolve_sheet(
                        sheets, member_profile["sheet"]
                    )

            if actual_sheet is None:
                issues.append(
                    f'**{file_name}** - no sheet like '
                    f'"{sheet_name or member_name}". '
                    f"Found: {', '.join(sheets[:8])}"
                )
                continue

            raw = read_sheet(payload, actual_sheet)

            spec = build_spec(raw, member_profile, overrides)

            missing = [
                field for field in ["key", "amount"]
                if spec.get(f"{field}_col") is None
                or spec[f"{field}_col"] not in raw.columns
            ]

            if missing:
                issues.append(
                    f"**{file_name}** / {actual_sheet} - no "
                    f"{' and '.join(missing)} column. Has: "
                    f"{', '.join(str(c) for c in raw.columns[:10])}"
                )
                continue

            tidy = extract(raw, spec, role, file_name, label,
                           identity.get("uid"))

            tidy["Sheet"] = actual_sheet

            quality.append({
                "File": file_name,
                "Account": label,
                "Role": role,
                "Sheet": actual_sheet,
                "Rows": len(raw),
                "Usable Rows": len(tidy),
                "Blank Keys": len(raw) - len(tidy),
                "Unparsed Dates": int(tidy["Timestamp"].isna().sum()),
                "Unsettled": int((~tidy["Settled"]).sum()),
                "Key Column": str(spec["key_col"]),
                "Amount Column": str(spec["amount_col"]),
            })

            frames.append(tidy)

        except Exception as exc:
            issues.append(
                f"**{file_name}** / {member_name} - "
                f"{type(exc).__name__}: {exc}"
            )

if issues:
    with st.expander(f"{len(issues)} file(s) had problems", expanded=True):
        for issue in issues:
            st.warning(issue)

if not frames:
    st.error("No file could be processed. Check the sheet and column "
             "selections in the sidebar.")
    st.stop()

pool = pd.concat(frames, ignore_index=True)

if "Master" not in set(pool["Role"]):
    st.error("The master file could not be processed, so there is nothing "
             "to correlate against.")
    st.stop()

master_label = pool.loc[pool["Role"] == "Master", "Account"].iloc[0]

# =========================================================
# APPLY FILTERS
# =========================================================

pool, pairs_df = deduplicate(pool)

pool = apply_counting(pool, drop_duplicates)

raw_total = float(pool["Abs USDT"].sum())

filtered = pool

if settled_only:
    filtered = filtered[filtered["Settled"]]

if min_amount > 0:
    filtered = filtered[filtered["Abs USDT"] >= min_amount]

currencies = sorted({
    str(c) for c in filtered["Currency"].dropna() if str(c) != "nan"
})

if currencies:
    chosen = st.sidebar.multiselect("Currency", currencies,
                                    default=currencies)
    if chosen:
        filtered = filtered[filtered["Currency"].isin(chosen)]

networks = sorted({
    str(n) for n in filtered["Network"].dropna() if str(n) != "nan"
})

if networks:
    chosen = st.sidebar.multiselect("Network", networks, default=networks)
    if chosen:
        filtered = filtered[filtered["Network"].isin(chosen)]

timestamps = filtered["Timestamp"].dropna()

if not timestamps.empty and timestamps.min().date() < timestamps.max().date():

    lo, hi = timestamps.min().date(), timestamps.max().date()

    span = st.sidebar.date_input("Date range", (lo, hi),
                                 min_value=lo, max_value=hi)

    if isinstance(span, (tuple, list)) and len(span) == 2:

        start = pd.Timestamp(span[0])
        end = pd.Timestamp(span[1]) + pd.Timedelta(days=1)

        keep = filtered["Timestamp"].isna() | filtered["Timestamp"].between(
            start, end
        )

        filtered = filtered[keep]

# Correlation only means something where master and a sub overlap.
master_keys = set(filtered.loc[filtered["Role"] == "Master", "Key"])
sub_keys = set(filtered.loc[filtered["Role"] == "Sub", "Key"])

common_keys = master_keys & sub_keys

common = filtered[filtered["Key"].isin(common_keys)]

# =========================================================
# HEADLINE FINDINGS
# =========================================================

summary_df = wallet_summary(common)
flows_df = flow_table(filtered)
graph, clusters_df = build_graph(filtered)
links_df = account_links(filtered)
patterns = find_patterns(common, window_hours, round_tolerance)
concentration_df = concentration(summary_df)

st.markdown("---")
st.subheader("Findings")

linked_accounts = set()

if not links_df.empty:
    linked_accounts = set(links_df["Account A"]) | set(links_df["Account B"])

total_volume = float(common["Volume USDT"].sum())

if not common_keys:
    st.warning(
        "No counterparty appears in both the master and any sub report "
        "under the current filters."
    )
else:
    one = len(common_keys) == 1

    headline = (
        f"**{len(common_keys)}** shared counterpart"
        f"{'y' if one else 'ies'} {'links' if one else 'link'} "
        f"**{max(len(linked_accounts), 2)}** accounts, moving "
        f"**{total_volume:,.2f} USDT** across "
        f"**{len(common):,}** transactions."
    )

    if not clusters_df.empty:
        headline += (
            f" Largest cluster: **{clusters_df.iloc[0]['Accounts']} "
            f"accounts** over "
            f"{clusters_df.iloc[0]['Counterparties']} counterpart"
            f"{'y' if clusters_df.iloc[0]['Counterparties'] == 1 else 'ies'}."
        )

    st.success(headline)

cols = st.columns(5)

cols[0].metric("Shared counterparties", len(common_keys))
cols[1].metric("Transactions", f"{len(common):,}")

if summary_df.empty:
    gross_in = gross_out = net = 0.0
else:
    gross_in = summary_df["Gross In USDT"].sum()
    gross_out = summary_df["Gross Out USDT"].sum()
    net = summary_df["Net USDT"].sum()

cols[2].metric("Gross in USDT", f"{gross_in:,.2f}")
cols[3].metric("Gross out USDT", f"{gross_out:,.2f}")
cols[4].metric("Net USDT", f"{net:,.2f}")

if not pairs_df.empty:
    st.info(
        f"{len(pairs_df)} transfer(s) appear in more than one report - the "
        "same movement recorded by both sides. "
        + ("They are counted once."
           if drop_duplicates
           else "De-duplication is off, so they are counted twice.")
    )

# =========================================================
# TABS
# =========================================================

tabs = st.tabs([
    "Counterparties", "Flows", "Clusters", "Timeline",
    "Patterns", "Per file", "Data quality",
])

# ---- Counterparties -------------------------------------------------

with tabs[0]:

    if summary_df.empty:
        st.info("Nothing to show under the current filters.")
    else:
        st.dataframe(summary_df, width="stretch", hide_index=True)

        st.subheader("Concentration")

        st.dataframe(concentration_df, width="stretch",
                     hide_index=True)

        top = summary_df.head(15).copy()
        top["Label"] = top["Counterparty"].map(short)

        st.altair_chart(
            alt.Chart(top).mark_bar().encode(
                x=alt.X("Total Volume USDT:Q", title="Total volume (USDT)"),
                y=alt.Y("Label:N", sort="-x", title="Counterparty"),
                tooltip=["Counterparty", "Gross In USDT", "Gross Out USDT",
                         "Net USDT", "Tx Count"],
            ).properties(height=420),
            width="stretch",
        )

# ---- Flows ----------------------------------------------------------

with tabs[1]:

    st.subheader(f"Value between {master_label} and each sub report")

    if flows_df.empty:
        st.info("No sub report shares a counterparty with the master.")
    else:
        st.dataframe(flows_df, width="stretch", hide_index=True)

    st.subheader("Transfers recorded by both sides")

    if pairs_df.empty:
        st.caption(
            "No transaction ID appears in two reports. Either the reports "
            "do not overlap on-chain, or this sheet has no usable ID column."
        )
    else:
        st.dataframe(pairs_df, width="stretch", hide_index=True)

# ---- Clusters -------------------------------------------------------

with tabs[2]:

    st.subheader("Account clusters")

    if clusters_df.empty:
        st.info("No counterparty is shared by two or more accounts.")
    else:
        st.dataframe(clusters_df, width="stretch", hide_index=True)

        st.subheader("Account-to-account links")

        st.dataframe(links_df, width="stretch", hide_index=True)

        st.caption(
            f"Graph: {graph.number_of_nodes()} nodes, "
            f"{graph.number_of_edges()} edges."
        )

# ---- Timeline -------------------------------------------------------

with tabs[3]:

    timed = common.dropna(subset=["Timestamp"])

    if timed.empty:
        st.info("No parseable timestamps in the selected data.")
    else:
        daily = (
            timed.set_index("Timestamp")
            .groupby("Flow")["Abs USDT"]
            .resample("D").sum()
            .reset_index()
        )

        st.altair_chart(
            alt.Chart(daily).mark_area(opacity=0.7).encode(
                x=alt.X("Timestamp:T", title="Date"),
                y=alt.Y("Abs USDT:Q", title="Volume (USDT)"),
                color=alt.Color("Flow:N", title="Direction"),
                tooltip=["Timestamp:T", "Flow:N", "Abs USDT:Q"],
            ).properties(height=320),
            width="stretch",
        )

        st.subheader("Busiest days")

        busiest = (
            timed.assign(Day=timed["Timestamp"].dt.date)
            .groupby("Day")
            .agg(Transactions=("Abs USDT", "size"),
                 Volume_USDT=("Abs USDT", "sum"),
                 Accounts=("Account", "nunique"))
            .sort_values("Volume_USDT", ascending=False)
            .head(20)
            .reset_index()
        )

        busiest["Volume_USDT"] = busiest["Volume_USDT"].round(2)

        st.dataframe(busiest, width="stretch", hide_index=True)

# ---- Patterns -------------------------------------------------------

with tabs[4]:

    st.subheader("Repeated identical amounts")

    st.caption("Three or more transfers of exactly the same size to one "
               "counterparty.")

    if patterns["repeats"].empty:
        st.info("None found.")
    else:
        st.dataframe(patterns["repeats"], width="stretch",
                     hide_index=True)

    st.subheader("Amounts just under a round number")

    st.caption("A classic structuring signal.")

    if patterns["near_round"].empty:
        st.info("None found.")
    else:
        st.dataframe(patterns["near_round"], width="stretch",
                     hide_index=True)

    st.subheader(f"Co-occurrence within {window_hours}h")

    st.caption("Two different accounts touching the same counterparty "
               "close together in time.")

    if patterns["cooccurrence"].empty:
        st.info("None found.")
    else:
        st.dataframe(patterns["cooccurrence"], width="stretch",
                     hide_index=True)

# ---- Per file -------------------------------------------------------

with tabs[5]:

    subs = filtered[filtered["Role"] == "Sub"]

    if subs.empty:
        st.info("No sub report rows survived the filters.")

    for account, chunk in subs.groupby("Account"):

        shared = master_keys & set(chunk["Key"])

        with st.expander(
                f"{account} - {len(shared)} shared counterpart"
                f"{'y' if len(shared) == 1 else 'ies'}"):

            if not shared:
                st.caption("No overlap with the master report.")
                continue

            scope = filtered[
                filtered["Key"].isin(shared)
                & filtered["Account"].isin({account, master_label})
            ]

            st.dataframe(wallet_summary(scope), width="stretch",
                         hide_index=True)

            st.caption("Transactions")

            st.dataframe(
                scope[["Account", "Key", "Signed USDT", "Counted",
                       "Currency", "Network", "Status", "Timestamp",
                       "TXID", "Row"]],
                width="stretch", hide_index=True,
            )

# ---- Data quality ---------------------------------------------------

with tabs[6]:

    st.subheader("What was loaded")

    st.dataframe(pd.DataFrame(quality), width="stretch",
                 hide_index=True)

    st.subheader("Effect of filters")

    st.dataframe(pd.DataFrame([
        {"Stage": "All rows loaded", "Rows": len(pool),
         "Volume USDT": round(raw_total, 2)},
        {"Stage": "After filters", "Rows": len(filtered),
         "Volume USDT": round(float(filtered["Volume USDT"].sum()), 2)},
        {"Stage": "Shared counterparties only", "Rows": len(common),
         "Volume USDT": round(total_volume, 2)},
    ]), width="stretch", hide_index=True)

    if issues:
        st.subheader("Skipped files")

        for issue in issues:
            st.warning(issue)

# =========================================================
# EXPORT
# =========================================================

st.markdown("---")

export_sheets = {
    "Counterparties": summary_df,
    "Concentration": concentration_df,
    "Flows": flows_df,
    "Matched_Transfers": pairs_df,
    "Clusters": clusters_df,
    "Account_Links": links_df,
    "Repeated_Amounts": patterns["repeats"],
    "Near_Round": patterns["near_round"],
    "Co_Occurrence": patterns["cooccurrence"],
    "Transactions": common.drop(columns=["Settled"], errors="ignore"),
    "Data_Quality": pd.DataFrame(quality),
}

try:
    workbook = write_workbook(export_sheets)

    st.download_button(
        label="Download Investigation Report",
        data=workbook,
        file_name="wallet_investigation_report.xlsx",
        mime=("application/vnd.openxmlformats-"
              "officedocument.spreadsheetml.sheet"),
    )

    st.success("Analysis complete.")

except Exception as exc:
    st.error(f"Could not build the Excel report: {type(exc).__name__}: {exc}")

st.markdown("---")

st.caption("Crypto Wallet Intelligence & Transaction Correlation System")
