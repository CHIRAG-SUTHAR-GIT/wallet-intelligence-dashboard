# Wallet Intelligence Dashboard

A Streamlit tool for correlating Binance account reports. It takes one
**master** report and any number of **sub** reports, finds the
counterparties they share, and reports the value that actually moved
between them.

## Running it

Double-click **`run.bat`** (Windows). It checks for Python, verifies the
dependencies, offers to install anything missing, and starts the app at
<http://localhost:8501>.

Or manually:

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Analysis types

| Mode | Sheet | Matches on | Direction |
| --- | --- | --- | --- |
| Withdrawal History | `Withdrawal History` | `Destination Address` | always outflow |
| Deposit History | `Deposit History` | `Deposit Address` | always inflow |
| Binance Pay | `Binance Pay` | `Counterparty Binance ID` | sign is in the amount |
| Attempted Withdrawal History | `Attempted Withdrawal History` | `Address` | always outflow |
| P2P | `P2P` | `Ad publisher ID` | sign is in the amount |
| Deposits + Withdrawals | both sheets | both address columns | per sheet |
| Custom | any sheet you pick | any column you pick | you choose |

Every column is resolved by alias and can be overridden in the sidebar,
so a report that renames `Address ` to `Deposit Address` still works.

## What it does that a plain overlap check does not

**De-duplicates transfers seen from both sides.** A withdrawal in the
master and the matching deposit in a sub report are one movement of
money recorded twice. Summing both inflates every total. Matching
transaction IDs are counted once — but the duplicate row is kept, not
deleted, because that pairing is the strongest evidence the two
accounts are linked.

Use the **Deposits + Withdrawals (combined)** mode for this: a
withdrawal and its matching deposit never share a sheet, so a
single-sheet mode cannot see the pair.

**Reports direction, not just volume.** Every counterparty gets gross
in, gross out and net. A wallet that took 50k in and sent 50k out nets
to zero and would otherwise look like nothing happened.

**Excludes unsettled rows.** Failed, cancelled and rejected
transactions are filtered out by default rather than summed as if they
had settled.

**Identifies accounts by who they are.** Each report's UID and email
are read from its `Customer Information` sheet, so tables read
`someone@example.com (100000001)` instead of
`report_BNB-000000_00000000000000_01_01_2026_03`.

## Tabs

- **Counterparties** — gross in/out/net per counterparty, concentration, top-15 chart
- **Flows** — directional value between the master and each sub, plus transfers recorded by both sides
- **Clusters** — a bipartite account↔counterparty graph, finding sub-to-sub links the master never touches
- **Timeline** — daily inflow/outflow and the busiest days
- **Patterns** — repeated identical amounts, amounts just under a round number, and two accounts hitting one counterparty close in time
- **Per file** — each sub report's overlap with the master, down to the source row
- **Data quality** — what loaded, what was skipped, and what each filter removed

Everything exports to a formatted 11-sheet Excel workbook.

## Appearance

Light and dark palettes are both defined in
[`.streamlit/config.toml`](.streamlit/config.toml). Light is the
default; switch from the app's **⋯ menu → Settings → Appearance**.

## A note on data

**Case files are deliberately excluded from this repository.** Binance
reports contain real account holder emails, UIDs, wallet addresses and
full transaction histories. `.gitignore` blocks `*.xlsx`, `*.xls` and
`*.csv` so they cannot be committed by accident. Keep them local.
