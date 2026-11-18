"""Run fingerprint: everything that decides a screener run's results, in one JSON file.

Why (2026-10): a Windows and a Mac machine on the same commit and the same
Rust engine produced different lists.  The engine is deterministic; the
differences came from each machine's own inputs -- the latest session it had
when it ran, missing sessions, an un-migrated store, a different history start,
a fundamentals file downloaded at another time.  Every run now writes
``<LOG_DIR>/run_fingerprint_<timestamp>.json`` describing those inputs, and

    python -m run_fingerprint a.json b.json

prints which fields differ between two runs (for example one from each
machine), so the cause of a difference is visible without re-running anything.

What is hashed: the engine reads the ``eod_data`` view, which is a pure
function of base tables -- the ``eod_prices`` table and ``securities`` in a
version 3 store (eod_prices is what the engine reads, raw x the ledger's
factors), ``raw_prices``, ``adjustment_periods`` and ``securities`` in a
version 2 store, the ``eod_prices`` table in a version 1 store or an external
(lake) export.  Hashing those base tables directly gives the same information
as hashing the view, without its ASOF join, in one scan (about 1 s for 3.8
million rows instead of 4-10 s).  ``store.engine_input_hash`` combines them into
one value.  The decision tables (``adjustment_ledger``, ``gap_history``,
``price_adjustments``, ...) are hashed in ``store.tables`` (the ledger without
its Decided_At time); ``maintenance_state`` records when, not what, and is only
counted.

The hashes are computed from text whose format does not depend on the platform
or the DuckDB version: dates as ISO text, prices as integers of 1/10^6, other
decimals as integers of 1/10^8 (NaN, infinity and out-of-range values spelled
out instead of failing), and the first 64 bits of each row's md5 summed modulo
2^64, so the row order never matters and a duplicated row is not cancelled out.

Fingerprinting never stops a run: every section that fails records its error
and logs a warning.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import platform
import re
import subprocess
import sys
import time
from datetime import date, datetime
from importlib import metadata
from pathlib import Path

import config

logger = logging.getLogger(__name__)

FINGERPRINT_VERSION = 2
EXCHANGES = ("NSE", "BSE")
_MOD64 = 1 << 64
# Price tables hashed per session in one scan (never through the view).
_PRICE_TABLES = ("raw_prices", "eod_prices")
# Columns left out of a table hash because they record when, not what.
_VOLATILE_COLUMNS = {"fetched_at", "applied_at", "decided_at"}
# Tables only counted: they record when the store was maintained, not what it holds.
_COUNTED_TABLES = ("maintenance_state",)
# Tables the eod_data view reads besides the price table (version 2; version 3 reads eod_prices and securities).
_VIEW_INPUT_TABLES = ("adjustment_periods", "securities")
_STORED_VIEW_INPUT_TABLES = ("securities",)
# Any other table larger than this is only counted: such tables (the lake
# export's eod_delivery, 3.8 million rows) hold columns the engine does not
# read, and hashing them would double the fingerprint's cost.
_HASH_ROW_LIMIT = 250_000
# The two output kinds and the column holding each list's breakout date.
_BREAKOUT_COLUMNS = ("Mark_Tight_Date", "breakout_date")
# Distributions whose versions are recorded; python, nse and httpx are
# informational (they do not compute prices).
_PACKAGES = ("pandas", "numpy", "duckdb", "nse", "httpx")
# Fingerprint fields that differ between two machines by nature and say
# nothing about the inputs; the comparison lists them separately.
INFORMATIONAL_KEYS = (
    "run.",
    "elapsed_seconds.",
    "packages.python",
    "packages.nse",
    "packages.httpx",
    "installed_packages.",
    "store.path",
    "store.size_bytes",
    "store.hash_seconds",
    "engine.path",
    "overrides.path",
    "fundamentals.path",
    "fundamentals.mtime_ist",
)


def _section(name: str, func, *args, **kwargs):
    """Run one fingerprint section; a failure is recorded and logged, never raised."""
    try:
        return func(*args, **kwargs)
    except Exception as error:  # noqa: BLE001 - fingerprinting must never stop a run
        logger.warning(f"  Run fingerprint: {name} unavailable ({type(error).__name__}: {error})")
        return {"error": f"{type(error).__name__}: {error}"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def engine_info(binary: Path) -> dict:
    """Version, full sha256 and path of the Rust engine (a non-executable file still gets its checksum)."""
    binary = Path(binary)
    try:
        version = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        version = f"version unavailable ({error})"
    return {
        "version": version or "version unavailable",
        "sha256": sha256_file(binary),
        "path": str(binary),
    }


def _run_section(timestamp: str) -> dict:
    local = datetime.now().astimezone()
    try:
        import tzlocal

        local_zone = tzlocal.get_localzone_name()
    except Exception:  # noqa: BLE001 - optional detail
        local_zone = None
    run_ist = datetime.strptime(timestamp, "%Y%m%d_%H%M%S").replace(tzinfo=config.MARKET_TIMEZONE)
    return {
        "timestamp": timestamp,
        "timestamp_ist": run_ist.isoformat(),
        "machine_local_time": local.isoformat(timespec="seconds"),
        "machine_tz": local.tzname(),
        "machine_tz_name": local_zone,
        "platform": platform.platform(),
        "python_executable": sys.executable,
    }


def _package_versions() -> dict:
    """Installed versions, read from package metadata (nothing is imported)."""
    versions = {"python": platform.python_version()}
    for name in _PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _normalize_package(name: str) -> str:
    """PEP 503 name: case and runs of '-', '_', '.' do not matter (PyYAML == pyyaml)."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _installed_packages() -> dict:
    """Every distribution installed for this interpreter: {normalized name: version}.

    Two different versions of one package (a stray dist-info) are kept as
    "v1 | v2", so a broken environment shows up in the comparison instead of
    being hidden.  One unreadable dist-info is skipped, never the whole list.
    """
    found: dict[str, set] = {}
    for dist in metadata.distributions():
        try:
            name = (dist.metadata or {}).get("Name")
            if not name:
                continue
            version = dist.version or "?"
        except Exception:  # noqa: BLE001 - one broken dist-info must not lose the list
            continue
        found.setdefault(_normalize_package(name), set()).add(str(version))
    return {name: " | ".join(sorted(versions)) for name, versions in sorted(found.items())}


def _settings(include_sme: bool, only_sme: bool) -> dict:
    from screening_readiness import EOD_READY_TIME

    return {
        "flag_lookback_days": config.FLAG_LOOKBACK_DAYS,
        "max_stale_sessions": config.MAX_STALE_SESSIONS,
        "include_sme": bool(include_sme),
        "only_sme": bool(only_sme),
        "eod_ready_time_ist": EOD_READY_TIME.strftime("%H:%M"),
    }


def _hex64(value) -> str:
    return f"{int(value or 0) % _MOD64:016x}"


def _digest16(lines) -> str:
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()[:16]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _row_hash_sql(expressions: list[str]) -> str:
    """SQL for a row's 64-bit md5 prefix; NULLs are spelled out because concat_ws skips them."""
    body = ", ".join(f"COALESCE(CAST({expr} AS VARCHAR), '\\N')" for expr in expressions)
    return f"CAST('0x' || substr(md5(concat_ws('|', {body})), 1, 16) AS UBIGINT)"


def _decimal_sql(column: str, scale: int, integer_type: str, limit: str) -> str:
    """A decimal column as an integer of 1/scale; NaN, infinity and huge values are spelled out."""
    return (
        f"CASE WHEN isfinite({column}) AND abs({column}) < {limit} "
        f"THEN CAST(CAST(round({column} * {scale}) AS {integer_type}) AS VARCHAR) "
        f"ELSE 'x' || CAST({column} AS VARCHAR) END"
    )


def _column_expression(name: str, data_type: str) -> str:
    """A platform- and version-stable text form of one column."""
    quoted = _quote(name)
    kind = data_type.upper()
    if kind in {"DOUBLE", "FLOAT", "REAL"} or kind.startswith("DECIMAL"):
        return _decimal_sql(f"CAST({quoted} AS DOUBLE)", 100000000, "HUGEINT", "1e20")
    if kind == "DATE":
        return f"strftime({quoted}, '%Y-%m-%d')"
    if kind.startswith("TIMESTAMP"):
        return f"strftime({quoted}, '%Y-%m-%d %H:%M:%S')"
    return quoted


def _table_digest(conn, table: str) -> dict:
    columns = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()
    used = [(name, kind) for name, kind in columns if name.lower() not in _VOLATILE_COLUMNS]
    rows = conn.execute(f"SELECT count(*) FROM {_quote(table)}").fetchone()[0]
    if not used or table in _COUNTED_TABLES:
        return {"rows": rows}
    if rows > _HASH_ROW_LIMIT and table not in _VIEW_INPUT_TABLES:
        return {"rows": rows, "hash": None, "note": f"over {_HASH_ROW_LIMIT} rows: counted, not hashed"}
    row_hash = _row_hash_sql([_column_expression(name, kind) for name, kind in used])
    rows, total = conn.execute(f"SELECT count(*), sum({row_hash}) FROM {_quote(table)}").fetchone()
    return {"rows": rows, "hash": _hex64(total), "columns": [name for name, _ in used]}


# One price row: symbol, prices to 1/10^6, volume.  The session date and the
# exchange are the GROUP BY keys and enter the exchange hash with the session.
_PRICE_ROW_HASH = _row_hash_sql(
    ["Symbol"]
    + [_decimal_sql(column, 1000000, "BIGINT", "1e12") for column in ("Open", "High", "Low", "Close")]
    + ["Volume"]
)


def _store_section(database: Path, external: bool = False) -> dict:
    """Describe the price store; ``external`` marks a store made elsewhere (``--skip-sync``)."""
    import duckdb

    from eod_store import schema_version

    database = Path(database)
    result: dict = {"path": str(database)}
    if not database.exists():
        result["error"] = "price store missing"
        return result
    result["size_bytes"] = database.stat().st_size
    conn = duckdb.connect(str(database), read_only=True)
    try:
        relations = dict(conn.execute(
            "SELECT table_name, table_type FROM information_schema.tables"
        ).fetchall())
        base = next((name for name in _PRICE_TABLES if relations.get(name) == "BASE TABLE"), None)
        version = schema_version(conn)
        if version == 3:
            # Adjusted prices stored beside the raw ones: hash what the engine reads.
            base, kind = "eod_prices", "v3"
        elif base == "raw_prices":
            kind = "v2"
        elif base == "eod_prices":
            # Adjusted prices stored as a table: an old store, or one exported by
            # the market-data lake and screened with --skip-sync.
            kind = "external" if external else "v1"
        else:
            kind = "empty"
        result["kind"] = kind
        result["schema_version"] = "external" if kind == "external" else version
        result["price_table"] = base

        exchanges: dict = {}
        sessions: dict = {}
        if base is not None:
            started = time.perf_counter()
            per_session = conn.execute(
                f"""
                SELECT Exchange, Date, count(*) AS n, sum({_PRICE_ROW_HASH}) AS h
                FROM {_quote(base)}
                WHERE Exchange IN ('NSE', 'BSE')
                GROUP BY Exchange, Date
                ORDER BY Exchange, Date
                """
            ).fetchall()
            symbols = dict(conn.execute(
                f"SELECT Exchange, count(DISTINCT Symbol) FROM {_quote(base)} "
                "WHERE Exchange IN ('NSE', 'BSE') GROUP BY Exchange"
            ).fetchall())
            result["hash_seconds"] = round(time.perf_counter() - started, 2)
            for exchange in EXCHANGES:
                mine = [row for row in per_session if row[0] == exchange]
                if not mine:
                    exchanges[exchange] = {"rows": 0, "sessions": 0}
                    continue
                sessions[exchange] = {row[1].isoformat(): f"{row[2]}:{_hex64(row[3])}" for row in mine}
                exchanges[exchange] = {
                    "rows": sum(row[2] for row in mine),
                    "sessions": len(mine),
                    "first_date": mine[0][1].isoformat(),
                    "last_date": mine[-1][1].isoformat(),
                    "symbols": symbols.get(exchange, 0),
                    "content_hash": _digest16(f"{day} {entry}" for day, entry in sessions[exchange].items()),
                }
        result["exchanges"] = exchanges

        tables: dict = {}
        for table in sorted(name for name, kind in relations.items() if kind == "BASE TABLE"):
            if table == base:
                tables[table] = {"rows": sum(info.get("rows", 0) for info in exchanges.values())}
            else:
                tables[table] = _section(f"table {table}", _table_digest, conn, table)
        result["tables"] = tables
        # One value for everything the eod_data view (what the engine reads) is computed from.
        view_inputs = {
            "kind": kind,
            "prices": {exchange: info.get("content_hash") for exchange, info in exchanges.items()},
        }
        for table in (_STORED_VIEW_INPUT_TABLES if kind == "v3" else _VIEW_INPUT_TABLES):
            view_inputs[table] = (tables.get(table) or {}).get("hash")
        result["engine_input_hash"] = _digest16([json.dumps(view_inputs, sort_keys=True)])
        if "exchange_actions" in relations:
            result["exchange_actions"] = dict(conn.execute(
                "SELECT Exchange, count(*) FROM exchange_actions GROUP BY Exchange ORDER BY Exchange"
            ).fetchall())
            # Which kind of listing differs, when two stores' table hashes do not match.
            cols = ["Symbol", "Code", "ISIN", "CAST(Ex_Date AS VARCHAR)", "Purpose", "Kind",
                    _decimal_sql("Factor", 10**9, "BIGINT", "1e7")]
            result["exchange_actions_detail"] = {
                f"{exchange}|{kind or ''}": {"rows": int(rows), "hash": _hex64(total)}
                for exchange, kind, rows, total in conn.execute(
                    f"SELECT Exchange, Kind, count(*), sum(CAST({_row_hash_sql(cols)} AS HUGEINT)) "
                    "FROM exchange_actions GROUP BY Exchange, Kind ORDER BY Exchange, Kind"
                ).fetchall()
            }
        else:
            result["exchange_actions"] = None
        # Per-session row counts and hashes: the comparison names the exact
        # sessions two stores disagree on.  Kept last so the JSON reads top-down.
        result["sessions"] = sessions
    finally:
        conn.close()
    return result


def _overrides_section() -> dict:
    # The same file eod_maintenance.overrides_file() reads (not imported: it pulls in pandas).
    path = config.EOD_METADATA_DIR / "corporate_action_overrides.csv"
    present = path.is_file()
    return {"path": str(path), "present": present, "sha256": sha256_file(path) if present else None}


def _fundamentals_section(path: Path) -> dict:
    path = Path(path)
    result: dict = {"name": path.name, "path": str(path), "present": path.is_file()}
    if not path.is_file():
        return result
    stat = path.stat()
    result["sha256"] = sha256_file(path)
    result["size_bytes"] = stat.st_size
    result["mtime_ist"] = datetime.fromtimestamp(stat.st_mtime, config.MARKET_TIMEZONE).isoformat(timespec="seconds")
    rows = 0
    isins: set[str] = set()
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        column = header.index("ISIN Code") if "ISIN Code" in header else None
        for row in reader:
            if not row:
                continue
            rows += 1
            if column is not None and column < len(row) and row[column].strip():
                isins.add(row[column].strip())
    result["rows"] = rows
    result["distinct_isins"] = len(isins) if column is not None else None
    return result


def _as_of_section(readiness, local_fallback: bool, sync_stopped: bool, warnings) -> dict:
    as_of = dict(getattr(readiness, "as_of_dates", {}) or {})
    target = getattr(readiness, "target_date", None)
    values = {exchange: value.isoformat() for exchange, value in sorted(as_of.items())}
    return {
        **values,
        "target": target.isoformat() if isinstance(target, date) else None,
        "screening_as_of": min(as_of.values()).isoformat() if as_of else None,
        "as_of_mismatch": len(set(as_of.values())) > 1,
        "local_fallback": bool(local_fallback),
        "sync_stopped": bool(sync_stopped),
        "warnings": list(warnings or ()),
    }


def collect(
    *,
    timestamp: str | None = None,
    database: Path | None = None,
    fundamentals: Path | None = None,
    engine: dict | None = None,
    readiness=None,
    local_fallback: bool = False,
    sync_stopped: bool = False,
    warnings=(),
    include_sme: bool = False,
    only_sme: bool = False,
    external_store: bool = False,
) -> dict:
    """Fingerprint the inputs of this run (before the engine runs)."""
    timestamp = timestamp or config.CURRENT_TIMESTAMP
    started = time.perf_counter()
    elapsed: dict = {}

    def timed(name, func, *args, **kwargs):
        t0 = time.perf_counter()
        value = _section(name, func, *args, **kwargs)
        elapsed[name] = round(time.perf_counter() - t0, 2)
        return value

    fingerprint = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "run": timed("run", _run_section, timestamp),
        "packages": timed("packages", _package_versions),
        "installed_packages": timed("installed_packages", _installed_packages),
        "engine": engine if engine is not None else timed("engine", engine_info, config.RUST_BINARY),
        "settings": _section("settings", _settings, include_sme, only_sme),
        "data_as_of": _section("as-of", _as_of_section, readiness, local_fallback, sync_stopped, warnings),
        "fundamentals": timed("fundamentals", _fundamentals_section, fundamentals or config.FUNDAMENTAL_FILE),
        "overrides": _section("overrides", _overrides_section),
        "store": timed("store", _store_section, database or config.DUCKDB_FILE, external_store),
        "outputs": None,
    }
    elapsed["total"] = round(time.perf_counter() - started, 2)
    fingerprint["elapsed_seconds"] = elapsed
    return fingerprint




def _outputs_section(out_dir: Path, timestamp: str) -> dict:
    out_dir = Path(out_dir)
    lists: dict = {}
    for path in sorted(out_dir.rglob(f"{timestamp}_*_qualified.csv")):
        key = path.parent.relative_to(out_dir).as_posix()
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            column = next((name for name in _BREAKOUT_COLUMNS if name in fields), None)
            entries = sorted(
                f"{row.get('symbol', '')}|{row.get(column, '') if column else ''}" for row in reader
            )
        lists[key] = {
            "file": path.name,
            "count": len(entries),
            "breakout_column": column,
            "hash": hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest(),
            "entries": entries,
        }
    return lists


def add_outputs(fingerprint: dict, out_dir: Path | None = None, timestamp: str | None = None) -> dict:
    """Record each written list's (symbol, breakout date) pairs after the engine finished."""
    t0 = time.perf_counter()
    fingerprint["outputs"] = _section(
        "outputs", _outputs_section, out_dir or config.OUT_DIR, timestamp or config.CURRENT_TIMESTAMP
    )
    fingerprint.setdefault("elapsed_seconds", {})["outputs"] = round(time.perf_counter() - t0, 2)
    return fingerprint


def fingerprint_path(timestamp: str | None = None) -> Path:
    return config.LOG_DIR / f"run_fingerprint_{timestamp or config.CURRENT_TIMESTAMP}.json"


def write(fingerprint: dict, path: Path | None = None) -> Path:
    path = Path(path or fingerprint_path(fingerprint.get("run", {}).get("timestamp")))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(fingerprint, indent=1, sort_keys=False, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def summary_lines(fingerprint: dict) -> list[str]:
    """The compact block printed in the run log."""
    def get(section, key, default="?"):
        value = fingerprint.get(section) or {}
        return value.get(key, default) if isinstance(value, dict) else default

    lines = []
    run = fingerprint.get("run") or {}
    lines.append(
        f"run {run.get('timestamp', '?')} IST | machine {run.get('machine_local_time', '?')} "
        f"{run.get('machine_tz_name') or run.get('machine_tz', '')} | {run.get('platform', '?')}"
    )
    packages = fingerprint.get("packages") or {}
    package_line = " | ".join(f"{name} {packages.get(name, '?')}" for name in ("python", "pandas", "numpy", "duckdb"))
    installed = fingerprint.get("installed_packages")
    if _package_list(installed) is not None:
        package_line += f" | {len(installed)} package{'' if len(installed) == 1 else 's'} installed"
    lines.append(package_line)
    lines.append(f"engine {get('engine', 'version')} | sha256 {get('engine', 'sha256')}")
    store = fingerprint.get("store") or {}
    if "error" in store:
        lines.append(f"store: {store['error']}")
    parts = [f"store {store.get('kind', '?')} #{store.get('engine_input_hash', '-')}"]
    for exchange, info in (store.get("exchanges") or {}).items():
        parts.append(
            f"{exchange} {info.get('rows', 0)} rows {info.get('sessions', 0)} sessions "
            f"{info.get('first_date', '-')}..{info.get('last_date', '-')} {info.get('symbols', 0)} symbols "
            f"#{info.get('content_hash', '-')}"
        )
    lines.append(" | ".join(parts))
    tables = store.get("tables") or {}
    factor_parts = [
        f"{name} {info.get('rows', '?')}" + (f" #{info['hash']}" if info.get("hash") else "")
        for name, info in tables.items() if isinstance(info, dict) and name not in _PRICE_TABLES
    ]
    actions = store.get("exchange_actions")
    overrides = fingerprint.get("overrides") or {}
    factor_parts.append(f"actions {actions}" if actions is not None else "actions absent")
    factor_parts.append(
        f"overrides {overrides['sha256'][:16]}" if overrides.get("present") else "overrides absent"
    )
    lines.append(" | ".join(factor_parts))
    fundamentals = fingerprint.get("fundamentals") or {}
    lines.append(
        f"fundamentals {fundamentals.get('name', '?')} | {fundamentals.get('rows', '?')} rows "
        f"{fundamentals.get('distinct_isins', '?')} ISINs | modified {fundamentals.get('mtime_ist', '?')} | "
        f"sha256 {str(fundamentals.get('sha256', '?'))[:16]}"
    )
    as_of = fingerprint.get("data_as_of") or {}
    lines.append(
        f"as-of NSE={as_of.get('NSE', '?')} BSE={as_of.get('BSE', '?')} target={as_of.get('target', '?')} | "
        f"local fallback: {'YES' if as_of.get('local_fallback') else 'no'} | "
        f"as-of mismatch: {'YES' if as_of.get('as_of_mismatch') else 'no'}"
    )
    outputs = fingerprint.get("outputs")
    if isinstance(outputs, dict) and "error" not in outputs:
        lines.append(" | ".join(
            f"{name} {info['count']} #{info['hash'][:16]}" for name, info in outputs.items()
        ) or "outputs: none written")
    lines.append(f"fingerprint took {(fingerprint.get('elapsed_seconds') or {}).get('total', '?')}s")
    return lines


# ---------------------------------------------------------------------------
# Comparison CLI
# ---------------------------------------------------------------------------
def _flatten(value, prefix: str = "") -> dict:
    if isinstance(value, dict):
        flat = {}
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}{key}."))
        return flat or {prefix.rstrip("."): {}}
    return {prefix.rstrip("."): value}


def _is_informational(key: str) -> bool:
    return any(key == marker.rstrip(".") or key.startswith(marker) for marker in INFORMATIONAL_KEYS)


def _short(values, limit: int = 12) -> str:
    values = sorted(values)
    shown = ", ".join(values[:limit])
    return shown + (f" (+{len(values) - limit} more)" if len(values) > limit else "")


def _package_list(value) -> dict | None:
    """The recorded package list, or None when absent (older code) or its section failed."""
    if not isinstance(value, dict) or "error" in value:
        return None
    return value


def _package_list_problem(fingerprint: dict) -> str | None:
    """Why a fingerprint has no package list to compare, or None when it has one."""
    value = fingerprint.get("installed_packages")
    if _package_list(value) is not None:
        return None
    if isinstance(value, dict):
        return f"its section failed ({value['error']})"
    return "written before the full list was recorded"


def compare_packages(a: dict, b: dict) -> list[str] | None:
    """Lines naming every installed package whose version differs or is on one side only.

    None when either fingerprint has no list (older code, or its section failed).
    """
    left, right = _package_list(a.get("installed_packages")), _package_list(b.get("installed_packages"))
    if left is None or right is None:
        return None
    lines = []
    for name in sorted(set(left) | set(right)):
        if name not in right:
            lines.append(f"{name}: A={left[name]}  B=<not installed>")
        elif name not in left:
            lines.append(f"{name}: A=<not installed>  B={right[name]}")
        elif left[name] != right[name]:
            lines.append(f"{name}: A={left[name]}  B={right[name]}")
    return lines


def compare(a: dict, b: dict) -> tuple[list[str], list[str]]:
    """Return (result-relevant differences, informational differences) as printable lines."""
    a, b = dict(a), dict(b)
    # The full package list is compared by compare_packages; the pinned
    # packages that compute prices stay under "packages" below.
    a.pop("installed_packages", None)
    b.pop("installed_packages", None)
    relevant: list[str] = []
    informational: list[str] = []

    # Per-session hashes: report dates rather than a thousand keys.
    sessions_a = (a.get("store") or {}).get("sessions") or {}
    sessions_b = (b.get("store") or {}).get("sessions") or {}
    for exchange in sorted(set(sessions_a) | set(sessions_b)):
        left, right = sessions_a.get(exchange, {}), sessions_b.get(exchange, {})
        only_a = set(left) - set(right)
        only_b = set(right) - set(left)
        changed = {day for day in set(left) & set(right) if left[day] != right[day]}
        if only_a:
            relevant.append(f"store.sessions.{exchange}: {len(only_a)} session(s) only in A: {_short(only_a)}")
        if only_b:
            relevant.append(f"store.sessions.{exchange}: {len(only_b)} session(s) only in B: {_short(only_b)}")
        if changed:
            relevant.append(f"store.sessions.{exchange}: {len(changed)} shared session(s) differ: {_short(changed)}")

    # Output lists: symbols gained and lost rather than a hash.
    outputs_a = a.get("outputs") if isinstance(a.get("outputs"), dict) else {}
    outputs_b = b.get("outputs") if isinstance(b.get("outputs"), dict) else {}
    for name in sorted(set(outputs_a) | set(outputs_b)):
        left = set((outputs_a.get(name) or {}).get("entries") or [])
        right = set((outputs_b.get(name) or {}).get("entries") or [])
        if name not in outputs_a or name not in outputs_b:
            relevant.append(f"outputs.{name}: only in {'A' if name in outputs_a else 'B'}")
        elif left != right:
            relevant.append(
                f"outputs.{name}: {len(left)} vs {len(right)} entries; "
                f"only in A: {_short(left - right) or '-'}; only in B: {_short(right - left) or '-'}"
            )

    for side in (a, b):
        if isinstance(side.get("store"), dict):
            side["store"] = {k: v for k, v in side["store"].items() if k != "sessions"}
        if isinstance(side.get("outputs"), dict):
            side["outputs"] = {
                name: {k: v for k, v in (info or {}).items() if k not in {"entries", "file"}}
                for name, info in side["outputs"].items()
            }
    flat_a, flat_b = _flatten(a), _flatten(b)
    for key in sorted(set(flat_a) | set(flat_b)):
        left, right = flat_a.get(key, "<absent>"), flat_b.get(key, "<absent>")
        if left == right:
            continue
        line = f"{key}: A={left!r}  B={right!r}"
        (informational if _is_informational(key) else relevant).append(line)
    return relevant, informational


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m run_fingerprint",
        description="Print which fields differ between two run fingerprint JSON files.",
    )
    parser.add_argument("a", type=Path, help="first run_fingerprint_*.json (A)")
    parser.add_argument("b", type=Path, help="second run_fingerprint_*.json (B)")
    parser.add_argument("--all", action="store_true", help="also print fields expected to differ (times, paths)")
    args = parser.parse_args(argv)

    try:
        a = json.loads(args.a.read_text(encoding="utf-8"))
        b = json.loads(args.b.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"Cannot read fingerprint: {error}", file=sys.stderr)
        return 2
    relevant, informational = compare(a, b)
    package_lines = compare_packages(a, b)
    print(f"A: {args.a}\nB: {args.b}")
    python_a = (a.get("packages") or {}).get("python", "?")
    python_b = (b.get("packages") or {}).get("python", "?")
    print(f"\nPython {python_a} vs {python_b}; ", end="")
    if package_lines is None:
        reasons = [f"{side} {problem}" for side, fp in (("A", a), ("B", b))
                   if (problem := _package_list_problem(fp))]
        print(f"installed packages not compared: {'; '.join(reasons)}.")
    elif package_lines:
        print(f"{len(package_lines)} installed package difference(s):")
        for line in package_lines:
            print(f"  {line}")
    else:
        print("installed packages: identical.")
    if relevant:
        print(f"\n{len(relevant)} difference(s) that can change results:")
        for line in relevant:
            print(f"  {line}")
    else:
        print("\nNo differences in the inputs or outputs that decide results.")
    if informational:
        if args.all:
            print(f"\n{len(informational)} field(s) expected to differ between runs:")
            for line in informational:
                print(f"  {line}")
        else:
            print(f"\n({len(informational)} time/path field(s) differ as expected; --all lists them.)")
    return 1 if relevant else 0


if __name__ == "__main__":
    sys.exit(main())
