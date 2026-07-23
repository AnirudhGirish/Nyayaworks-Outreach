"""Batched Google Sheets I/O for the NyayaWorks pipeline.

The Sheet is the single source of truth. All reads and writes are batched to
stay well clear of Google API rate limits (§3). The :class:`SheetsClient`
ABC lets the rest of the pipeline be tested against an in-memory backend
without touching the network.

Every gspread call is wrapped with a timeout and exception handler so a hung
or rate-limited Sheets API raises a clean RuntimeError instead of crashing
the whole cron run.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

import config

T = TypeVar("T")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sheets_call(fn: Callable[..., T], *args, **kwargs) -> T:
    """Wrap a gspread call with timeout enforcement and exception handling.

    Catches gspread APIError, google-auth transport errors, and generic
    connection errors, re-raising as RuntimeError so callers can log it.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        raise RuntimeError(f"Sheets API call failed ({fn.__name__}): {exc}") from exc


def _coerce_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a lead row loaded from the sheet into the canonical schema.

    Sheets return everything as strings; we restore the typed values the rest
    of the pipeline expects (booleans, ints, JSON blobs).
    """
    out: dict[str, Any] = {col: "" for col in config.LEADS_COLUMNS}
    out.update(row)
    out["row_id"] = out.get("row_id") or ""
    out["attempts"] = _to_int(out.get("attempts"))
    out["do_not_contact"] = _to_bool(out.get("do_not_contact"))
    if out.get("research_json") and isinstance(out["research_json"], str):
        try:
            out["research_json"] = json.loads(out["research_json"])
        except (json.JSONDecodeError, TypeError):
            out["research_json"] = None
    return out


def _to_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


class SheetsClient(ABC):
    """Abstract backend for Sheet access. Enables mocked testing."""

    @abstractmethod
    def get_leads(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def batch_update_leads(self, rows: list[dict[str, Any]]) -> None: ...

    @abstractmethod
    def get_control(self) -> dict[str, Any]: ...

    @abstractmethod
    def set_control(self, updates: dict[str, Any]) -> None: ...


class GspreadClient(SheetsClient):
    """Concrete gspread-backed implementation using a service account."""

    def __init__(
        self,
        sheet_id: str | None = None,
        credentials_path: str | None = None,
        credentials_json: str | None = None,
    ) -> None:
        import gspread

        self.sheet_id = sheet_id or config.SHEET_ID
        if not self.sheet_id:
            raise RuntimeError("SHEET_ID is not configured")

        creds_path = credentials_path or config.GOOGLE_CREDENTIALS_PATH
        creds_json = credentials_json or config.GOOGLE_SERVICE_ACCOUNT_JSON
        if creds_json:
            info = json.loads(creds_json)
            self.gc = _sheets_call(gspread.service_account_from_dict, info)
        elif creds_path:
            self.gc = _sheets_call(gspread.service_account, filename=creds_path)
        else:
            raise RuntimeError(
                "No Google credentials: set GOOGLE_SERVICE_ACCOUNT_JSON or "
                "GOOGLE_CREDENTIALS_PATH"
            )

        self.gc.set_timeout(config.SHEETS_TIMEOUT)
        self.sh = _sheets_call(self.gc.open_by_key, self.sheet_id)
        # Worksheets are bound lazily after setup_sheet() guarantees they exist.
        self.leads_ws = self._bind(config.LEADS_TAB)
        self.control_ws = self._bind(config.CONTROL_TAB)

    def _bind(self, title: str):
        try:
            return _sheets_call(self.sh.worksheet, title)
        except RuntimeError:
            return None

    # -- bootstrap ---------------------------------------------------------
    def setup_sheet(self) -> None:
        """Create the tabs + header rows if they don't already exist.

        Safe to call every run: it only adds what is missing (idempotent).
        """
        self.leads_ws = self._ensure_tab(
            config.LEADS_TAB, config.LEADS_COLUMNS
        )
        self.control_ws = self._ensure_tab(
            config.CONTROL_TAB, config.CONTROL_COLUMNS
        )
        self._ensure_control_defaults()

    def _ensure_tab(self, title: str, header: list[str]):
        try:
            ws = _sheets_call(self.sh.worksheet, title)
        except RuntimeError:
            ws = _sheets_call(self.sh.add_worksheet, title=title, rows=100, cols=len(header))
        existing = _sheets_call(ws.row_values, 1)
        if not existing or all(v == "" for v in existing):
            _sheets_call(ws.update, "A1", [header])
        return ws

    def _ensure_control_defaults(self) -> None:
        """Write sensible defaults into the control row if cells are empty."""
        header = _sheets_call(self.control_ws.row_values, 1)
        values = _sheets_call(self.control_ws.row_values, 2)
        defaults = {
            "daily_cap": "5",
            "sent_today": "0",
            "date_reset_at": "",
            "send_window_start": "9",
            "send_window_end": "18",
            "is_locked": "",
            "last_error": "",
        }
        updates = []
        for key, val in defaults.items():
            if key in header:
                col = header.index(key) + 1
                current = values[col - 1] if col - 1 < len(values) else ""
                if current in ("", None):
                    updates.append({"range": f"{_col_letter(col)}2", "values": [[val]]})
        if updates:
            _sheets_call(self.control_ws.batch_update, updates)

    # -- leads -------------------------------------------------------------
    def get_leads(self) -> list[dict[str, Any]]:
        records = _sheets_call(self.leads_ws.get_all_records)
        return [_coerce_row(r) for r in records]

    def batch_update_leads(self, rows: list[dict[str, Any]]) -> None:
        """Batch-update whole rows for the given leads (matched by row_id)."""
        if not rows:
            return
        header = _sheets_call(self.leads_ws.row_values, 1)
        all_records = _sheets_call(self.leads_ws.get_all_records)
        by_id = {str(r.get("row_id")): r for r in all_records}
        updates = []
        for row in rows:
            rid = str(row.get("row_id"))
            existing = by_id.get(rid)
            if existing is None:
                continue
            row_index = all_records.index(existing) + 2  # +1 header, +1 1-based
            values = []
            payload = dict(existing)
            payload.update(row)
            for col in header:
                val = payload.get(col, "")
                if col == "research_json" and isinstance(val, (dict, list)):
                    val = json.dumps(val)
                elif col == "do_not_contact":
                    val = "TRUE" if val else "FALSE"
                values.append(val)
            updates.append({"range": f"A{row_index}", "values": [values]})
        if updates:
            _sheets_call(self.leads_ws.batch_update, updates)

    # -- control -----------------------------------------------------------
    def get_control(self) -> dict[str, Any]:
        """Control is a header row (row 1) with values in row 2."""
        header = _sheets_call(self.control_ws.row_values, 1)
        values = _sheets_call(self.control_ws.row_values, 2)
        ctrl: dict[str, Any] = {}
        for i, key in enumerate(header):
            if key:
                ctrl[key] = values[i] if i < len(values) else ""
        return ctrl

    def set_control(self, updates: dict[str, Any]) -> None:
        header = _sheets_call(self.control_ws.row_values, 1)
        cells = []
        for key, value in updates.items():
            if key in header:
                col_index = header.index(key) + 1
                cells.append(gspread_cell(2, col_index, str(value)))
        if cells:
            _sheets_call(self.control_ws.update_cells, cells)


def gspread_cell(row: int, col: int, value: str):
    """Build a gspread Cell object (imported lazily to keep unit tests light)."""
    from gspread import Cell

    return Cell(row, col, value)


def _col_letter(col: int) -> str:
    """1 -> A, 27 -> AA (minimal A1 notation helper)."""
    s = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return s
