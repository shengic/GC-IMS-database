"""Pure, DB-free .mea parsing. Version 1.0.0.

Reference implementation of DESIGN §2b (split_mea), §2c/§2f (aliases,
absence-tolerance), §5b (windowed RIP detection), §19 (leniency vs
silent-failure boundary). Unit-testable in isolation; reused by the
Tk viewer for full-resolution reads so read/write sides can never drift.
"""
from __future__ import annotations
import hashlib
import re
from datetime import datetime
import numpy as np

PRINTABLE = set(range(32, 256)) | {9, 10, 13}


class MeaParseError(Exception):
    """Raised when a file cannot be split unambiguously. Ingest maps
    this to ingest_log result 'parse_error' (§19: fail loud, never guess)."""


def split_mea(raw: bytes):
    """Split .mea into (header_dict, int16 matrix, meta).
    PRIMARY: arithmetic back-calc from Chunks count x Chunk sample count x 2.
    SECONDARY: first non-printable byte, cross-check within 4 bytes."""
    head_txt = raw[:200_000].decode("latin-1", errors="replace")
    m1 = re.search(r"Chunks count\s*=\s*(\d+)", head_txt)
    m2 = re.search(r"Chunk sample count\s*=\s*(\d+)", head_txt)
    if not (m1 and m2):
        raise MeaParseError("geometry keys not found in leading text")
    n_spec, n_drift = int(m1.group(1)), int(m2.group(1))
    boundary = len(raw) - n_spec * n_drift * 2
    if boundary <= 0:
        raise MeaParseError(f"declared matrix larger than file: boundary={boundary}")

    heur = next((i for i, b in enumerate(raw[:300_000]) if b not in PRINTABLE), -1)
    if heur < 0:
        raise MeaParseError("no non-printable byte in first 300 kB")
    delta = boundary - (heur + 1)
    if abs(delta) > 4:
        raise MeaParseError(f"boundary mismatch: arith={boundary} heur={heur} delta={delta}")

    header: dict[str, str] = {}
    for line in raw[:boundary].decode("latin-1").split("\n"):
        if "=" in line:
            k, v = line.split("=", 1)
            header[k.strip()] = v.strip()

    matrix = np.frombuffer(raw[boundary:], dtype="<i2").reshape(n_spec, n_drift)
    return header, matrix, {"header_bytes": boundary, "boundary_delta": delta}


RIP_ROW0_SKIP = 200


def find_rip(matrix: np.ndarray, start: int = RIP_ROW0_SKIP):
    """VOCal-compatible RIP finder (matches GC-IMS-PEAK/rip.py find_rip).
    Argmax over the first spectrum (RT=0 row) after skipping the first
    `start` drift samples (default 200, inherited from VOCal's
    CleanMEA.getRIP). Signed value from row0 -> polarity; magnitude
    vs. row0-median -> weak-RIP warning (per §5b/§19). The 200-sample
    skip also rejects leading-transient hijacking (the drift-18 case)."""
    if matrix.ndim != 2 or matrix.shape[1] <= start:
        raise ValueError(f"matrix unsuitable for RIP: shape={matrix.shape}, start={start}")
    row0 = matrix[0, :]
    tail = row0[start:]
    idx_local = int(np.argmax(np.abs(tail)))
    rip_index = start + idx_local
    signed = float(row0[rip_index])
    polarity = "negative" if signed < 0 else "positive"
    med = float(np.median(np.abs(tail)))
    weak = abs(signed) < 3.0 * med
    return rip_index, signed, polarity, weak


# --- header value coercion helpers (§2c: multi-format tolerance) ---
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _unquote(s):
    if s is None:
        return None
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def _num(s):
    """Extract first number from a value like '150 [kHz]'. None if absent/'xxx'/'off'."""
    if s is None:
        return None
    s = _unquote(s)
    if s.lower() in ("off", "xxx", ""):
        return None
    m = _NUM_RE.search(s)
    return float(m.group(0)) if m else None


def _parse_date(s):
    """§2c: try ISO then legacy 'Apr 25 2014' style."""
    if s is None:
        return None
    s = _unquote(s).strip()
    if s.lower() in ("", "off", "xxx"):
        return None
    for fmt in ("%Y-%m-%d", "%b %d %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_ts(s):
    if s is None:
        return None
    s = _unquote(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _first(header, *keys):
    for k in keys:
        if k in header:
            return header[k]
    return None


# --- HEADER_KEY_ALIASES per DESIGN §2f table ---

def promote(header: dict) -> dict:
    """Return canonical column-name -> value dict for a header. Absent keys => None."""
    p = {}
    p["machine_type"] = _unquote(_first(header, "Machine type"))
    p["machine_serial"] = _unquote(_first(header, "Machine serial"))
    p["machine_name"] = _unquote(_first(header, "Machine name"))
    p["adio_serial"] = _unquote(_first(header, "ADIO serial"))
    p["firmware_version"] = _unquote(_first(header, "Firmware version", "Firmware Version"))
    p["firmware_date"] = _parse_date(_first(header, "Firmware date", "Firmware Date"))
    dtl = _num(_first(header, "nom Drift Tube Length"))
    p["drift_tube_um"] = int(dtl) if dtl is not None else None
    p["drift_voltage_v"] = _num(_first(header, "Sensor drift voltage", "nom Drift Potential Difference"))
    p["sensor_data"] = _unquote(_first(header, "Sensor data"))

    p["program_raw"] = _unquote(_first(header, "Program"))
    p["gc_column"] = _unquote(_first(header, "GC Column"))
    drift_gas_raw = _first(header, "Drift Gas")
    if drift_gas_raw is None:
        epc = _first(header, "EPC gas settings")
        if epc is not None:
            m = re.search(r"IMS:\s*([^,\s]+)", _unquote(epc))
            drift_gas_raw = m.group(1) if m else None
    p["drift_gas"] = _unquote(drift_gas_raw) if drift_gas_raw else None
    p["flow_ims_setpoint_ml_min"] = _num(_first(
        header, "Start flow IMS", "Start flow1", "Flow IMS setpoint", "Flow1 setpoint"))
    p["flow_gc_setpoint_ml_min"] = _num(_first(
        header, "Start flow GC", "Start flow2", "Flow GC setpoint", "Flow2 setpoint",
        "Pump 1 flow setpoint", "Start flow pump 1"))

    temps = {}
    for i in range(1, 10):
        v = _first(header, f"Temp {i} setpoint", f"Start temp {i}")
        if v is not None:
            temps[f"T{i}"] = _unquote(v)
    p["temp_setpoints_c"] = temps if temps else None

    p["measured_at"] = _parse_ts(_first(header, "Timestamp"))
    p["sample_name"] = _unquote(_first(header, "Sample"))
    p["sample_class"] = _unquote(_first(header, "Class"))
    p["status"] = _unquote(_first(header, "Status"))
    ca = _num(_first(header, "Chunk averages"))
    p["chunk_averages"] = int(ca) if ca is not None else None
    p["sample_rate_khz"] = _num(_first(header, "Chunk sample rate", "Sample rate"))
    p["trig_repetition_ms"] = _num(_first(
        header, "Chunk trigger repetition", "Trig. repetition time"))
    # ambient_pressure_kpa is derived from Pressure Ambient telemetry mean in the ingest step
    p["ambient_pressure_kpa"] = None
    return p


def sample_type_from_name(name):
    """§3 measurement.sample_type classification. Conservative pattern match."""
    if not name:
        return "unknown"
    n = name.lower()
    if "blank" in n or "blind" in n:
        return "blank"
    if "calib" in n or "calibration" in n:
        return "standard"
    if re.search(r"\bstd\b|\bstandard\b", n):
        return "standard"
    if re.search(r"\bqc\b|quality", n):
        return "qc"
    return "sample"


def parse_program(program_raw):
    """Extract Name=`X` from Program string per §19.
    Returns (name, ok). ok=False -> use '(unparsed)' + warning."""
    if not program_raw:
        return None, True
    m = re.search(r"Name=`([^`]+)`", program_raw)
    if m:
        return m.group(1), True
    return "(unparsed)", False


def program_hash(program_raw, gc_column, drift_gas) -> str:
    """§3 gc_method dedup: sha256(program_raw + column + gas)."""
    parts = [program_raw or "", gc_column or "", drift_gas or ""]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# Header key -> canonical telemetry series name.
# Order does not matter (each key checked independently).
TELEMETRY_ALIASES = {
    "Flow Epc 1": "flow_ims",
    "Flow EPC IMS": "flow_ims",
    "Flow Epc 2": "flow_gc",
    "Flow EPC GC": "flow_gc",
    "Pressure Epc 1": "press_ims",
    "Pressure EPC IMS": "press_ims",
    "Pressure Epc 2": "press_gc",
    "Pressure EPC GC": "press_gc",
    "Pressure Ambient": "press_ambient",
    "Pump 1 flow": "pump1_flow",
    "Pump 1 pressure": "pump1_pressure",
}


def parse_telemetry(header):
    """Extract array-valued telemetry keys. Non-numeric tokens ('xxx') dropped.
    Returns list of (series_name, [float, ...])."""
    out = []
    for key, series in TELEMETRY_ALIASES.items():
        if key not in header:
            continue
        raw = _unquote(header[key])
        values = []
        for tok in raw.split():
            try:
                values.append(float(tok))
            except ValueError:
                pass
        if values:
            out.append((series, values))
    return out
