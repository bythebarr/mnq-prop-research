"""SYNTHETIC FAKE one-minute bars - for testing software plumbing ONLY.

!!! This data is randomly generated. It is NOT market data. !!!
!!! It must NEVER be used as evidence about any strategy.   !!!

The price path is a structureless random walk on a 0.25-point grid: there is
deliberately nothing to "find" in it. It exists only so that the data
contract, time-zone handling, duplicate / bad-OHLC / missing-bar detection and
hashing can be exercised without real data.

Every row is labelled: ``source`` is ``SYNTHETIC_FAKE_DATA_NOT_MARKET_DATA``
``contract`` is ``FAKE-MNQ`` and ``instrument_id`` is 0. Output files also get a sidecar
``.manifest.json`` repeating the warning.

The same seed and parameters always produce identical data (deterministic).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from mnq_research.data_contracts import EXCHANGE_TIMEZONE, REQUIRED_COLUMNS, cme_trading_date
from mnq_research.hashing import hash_dataframe, hash_file_bytes

SYNTHETIC_SOURCE = "SYNTHETIC_FAKE_DATA_NOT_MARKET_DATA"
SYNTHETIC_CONTRACT = "FAKE-MNQ"
SYNTHETIC_INSTRUMENT_ID = 0  # fake; real IDs come from Databento symbology
SYNTHETIC_WARNING = (
    "SYNTHETIC FAKE DATA - randomly generated for software testing only. "
    "NOT market data. Must never be used as evidence of strategy performance."
)
_PLUMBING_TICK = 0.25  # a price grid for realism of the plumbing only


@dataclass(frozen=True)
class SyntheticSpec:
    first_trading_date: str = "2024-01-08"  # a Monday
    n_trading_days: int = 3
    seed: int = 20240108
    start_price: float = 10_000.0


def _session_minutes_utc(trading_date: pd.Timestamp) -> pd.DatetimeIndex:
    """Bar-start minutes of one regular Globex session, in UTC.

    Session for trading date D: 17:00 Chicago on D-1 up to the bar starting
    15:59 Chicago on D (Monday's session opens Sunday 17:00).
    """
    start = pd.Timestamp(trading_date.date() - pd.Timedelta(days=1)).tz_localize(EXCHANGE_TIMEZONE) + pd.Timedelta(hours=17)
    end = pd.Timestamp(trading_date.date()).tz_localize(EXCHANGE_TIMEZONE) + pd.Timedelta(hours=15, minutes=59)
    return pd.date_range(start, end, freq="min").tz_convert("UTC")


def generate_synthetic_bars(spec: SyntheticSpec = SyntheticSpec()) -> pd.DataFrame:
    """Generate clean, contract-compliant FAKE bars for ``spec.n_trading_days`` weekdays."""
    if spec.n_trading_days < 1:
        raise ValueError("n_trading_days must be at least 1")
    first = pd.Timestamp(spec.first_trading_date)
    if first.weekday() >= 5:
        raise ValueError(f"first_trading_date {spec.first_trading_date} is a weekend; choose a weekday")
    trading_dates = pd.bdate_range(first, periods=spec.n_trading_days)
    sessions = [_session_minutes_utc(d) for d in trading_dates]
    stamps = sessions[0].append(sessions[1:]).as_unit("ns")
    n = len(stamps)

    rng = np.random.default_rng(spec.seed)
    steps = rng.integers(-4, 5, size=n) * _PLUMBING_TICK  # symmetric: no drift, no pattern
    close = spec.start_price + np.cumsum(steps)
    open_ = np.concatenate([[spec.start_price], close[:-1]])
    high = np.maximum(open_, close) + rng.integers(0, 3, size=n) * _PLUMBING_TICK
    low = np.minimum(open_, close) - rng.integers(0, 3, size=n) * _PLUMBING_TICK
    volume = rng.integers(1, 200, size=n)
    if (low <= 0).any():
        raise ValueError("start_price too low for this many bars; increase start_price")

    ts = pd.Series(stamps)
    df = pd.DataFrame(
        {
            "timestamp_utc": ts,
            "timestamp_exchange": ts.dt.tz_convert(EXCHANGE_TIMEZONE),
            "trading_date": cme_trading_date(ts).dt.date,
            "contract": SYNTHETIC_CONTRACT,
            "instrument_id": SYNTHETIC_INSTRUMENT_ID,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume.astype("int64"),
            "source": SYNTHETIC_SOURCE,
            "source_timezone": "UTC",
            # Fixed (not wall-clock) so output is reproducible; after every bar closed.
            "ingestion_timestamp_utc": ts.iloc[-1] + pd.Timedelta(minutes=1),
        }
    )
    df.attrs["warning"] = SYNTHETIC_WARNING
    return df[list(REQUIRED_COLUMNS)]


def inject_defects(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, object]]:
    """Return a copy of clean synthetic bars with KNOWN defects planted.

    Used to demonstrate that validation catches each defect. Positions are
    fixed, so the result is deterministic. Requires at least ~300 bars.
    """
    if len(df) < 300:
        raise ValueError("need at least 300 bars to inject defects")
    bad = df.copy().reset_index(drop=True)
    # Impossible OHLC: high below low.
    bad.loc[20, "high"] = bad.loc[20, "low"] - 1.0
    # Nonpositive price (internally consistent OHLC, but zero).
    bad.loc[30, ["open", "high", "low", "close"]] = 0.0
    # Unsorted: swap two neighbouring bars.
    order = list(range(len(bad)))
    order[200], order[201] = 201, 200
    bad = bad.iloc[order].reset_index(drop=True)
    # Missing bars: remove five consecutive minutes.
    dropped = bad.loc[100:104, "timestamp_utc"].tolist()
    bad = bad.drop(index=range(100, 105))
    # Duplicate: repeat the bar at position 10 immediately after itself.
    bad = pd.concat([bad.iloc[:11], bad.iloc[[10]], bad.iloc[11:]], ignore_index=True)
    description = {
        "impossible_ohlc_rows": 1,
        "nonpositive_price_rows": 1,
        "swapped_rows_for_unsorted": 1,
        "missing_bars": len(dropped),
        "missing_bar_timestamps_utc": [t.isoformat() for t in dropped],
        "duplicate_rows_added": 1,
    }
    return bad, description


def write_synthetic_dataset(df: pd.DataFrame, out_path: str | Path, spec: SyntheticSpec, defects: dict | None = None) -> Path:
    """Write bars to Parquet plus a sidecar manifest carrying the FAKE warning."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    manifest = {
        "WARNING": SYNTHETIC_WARNING,
        "generator": "mnq_research.synthetic_data",
        "parameters": spec.__dict__,
        "defects_injected": defects,
        "rows": len(df),
        "file_sha256": hash_file_bytes(out_path),
        "content_sha256": hash_dataframe(df),
    }
    manifest_path = out_path.with_suffix(out_path.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
    return manifest_path
