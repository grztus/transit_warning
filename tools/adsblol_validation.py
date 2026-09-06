"""Offline aircraft-only comparison; no runtime import, source fusion or prediction."""
from bisect import bisect_left
import csv
import datetime as dt
import gzip
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
FIELDS = ("lat", "lon", "baro_ft", "geom_ft", "gs_kt", "track_deg", "baro_rate_fpm", "geom_rate_fpm")


def number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def timestamp(value, timezone=None):
    if number(value) is not None:
        return float(value)
    text = str(value).replace("Z", "+00:00").replace("/", "-")
    date = dt.datetime.fromisoformat(text)
    if date.tzinfo is None:
        if not timezone:
            raise ValueError("Naive timestamp requires --sbs-timezone")
        zone = ZoneInfo(timezone)
        options = {date.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                   for fold in (0, 1)
                   if date.replace(tzinfo=zone, fold=fold).astimezone(UTC).astimezone(zone).replace(tzinfo=None) == date}
        if len(options) != 1:
            raise ValueError("Ambiguous/nonexistent local timestamp")
        date = options.pop()
    return date.timestamp()


def utc(seconds):
    return dt.datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def circular_difference(left, right):
    return (left - right + 180) % 360 - 180


def read_json(path):
    data = Path(path).read_bytes()
    return json.loads(gzip.decompress(data) if data.startswith(b"\x1f\x8b") else data)


class Series:
    """Independent timestamped fields; missing values and gaps are not bridged."""
    def __init__(self, values):
        merged = {}
        for t, value in values:
            if t in merged and merged[t] != value:
                raise ValueError("Conflicting samples at the same timestamp")
            merged[t] = value
        self.points = sorted(merged.items())
        self.times = [p[0] for p in self.points]

    def at(self, t, max_gap, circular=False):
        i = bisect_left(self.times, t)
        if i < len(self.times) and self.times[i] == t:
            return self.points[i][1]
        if i == 0 or i == len(self.times):
            return None
        a, b = self.points[i-1], self.points[i]
        if a[1] is None or b[1] is None or b[0] - a[0] > max_gap:
            return None
        delta = circular_difference(b[1], a[1]) if circular else b[1] - a[1]
        result = a[1] + (t-a[0]) / (b[0]-a[0]) * delta
        return result % 360 if circular else result


def sample(series, t, gap):
    result = {key: series.get(key, Series([])).at(t, gap, key in ("track_deg", "lon", "sbs_track_deg"))
              for key in (*FIELDS, "sbs_track_deg", "sbs_rate_fpm")}
    if result["lon"] is not None:
        result["lon"] = (result["lon"] + 180) % 360 - 180
    return result


def load_trace(path, icao):
    data = read_json(path)
    if str(data.get("icao", "")).upper() != icao.upper():
        raise ValueError("Trace ICAO does not match selected encounter")
    base = timestamp(data["timestamp"])
    values = {key: [] for key in FIELDS}
    for row in data["trace"]:
        if not isinstance(row, list) or len(row) < 8 or number(row[0]) is None:
            raise ValueError("Unsupported trace row")
        if type(row[6]) is not int:
            raise ValueError("Trace flags must be an integer")
        t, flags = base + row[0], row[6]
        v = {key: None for key in FIELDS}
        v.update(lat=number(row[1]), lon=number(row[2]), gs_kt=number(row[4]),
                 track_deg=number(row[5]) if row[3] != "ground" else None)
        v["geom_ft" if flags & 8 else "baro_ft"] = number(row[3])
        v["geom_rate_fpm" if flags & 4 else "baro_rate_fpm"] = number(row[7])
        extra = row[8] if len(row) > 8 and isinstance(row[8], dict) else {}
        for key, index, extra_key in (("geom_ft", 10, "alt_geom"), ("geom_rate_fpm", 11, "geom_rate")):
            value = number(row[index]) if len(row) > index else number(extra.get(extra_key))
            if value is not None:
                v[key] = value
        for key, extra_key in (("baro_ft", "alt_baro"), ("baro_rate_fpm", "baro_rate")):
            if v[key] is None:
                v[key] = number(extra.get(extra_key))
        if v["lat"] is not None and abs(v["lat"]) > 90 or v["lon"] is not None and abs(v["lon"]) > 180:
            raise ValueError("Invalid trace position")
        # A stale position/new-leg boundary must not create a synthetic bridge.
        if flags & 3:
            for key in values:
                values[key].append((t - 0.000001, None))
        if flags & 1:
            v["lat"] = v["lon"] = None
        for key in values:
            values[key].append((t, v[key]))
    return {key: Series(points) for key, points in values.items()}


def resolve_capture(path, encounter_id=None):
    path = Path(path).resolve()
    manifest_path = path / "capture_manifest.json" if path.is_dir() else path
    if path.is_dir() and not manifest_path.exists() and (path / "manifest.json").is_file():
        manifest_path = path / "manifest.json"
    manifest = read_json(manifest_path)
    if manifest.get("storage_mode") == "FULL_REFERENCE":
        raise ValueError(
            "FULL-reference-only captures are not supported by A4.0. "
            "Supply a self-contained candidate capture or encounter manifest. "
            "Shared FULL RAW streams require a separate UTC timing adapter.")
    if "latest_prediction" in manifest:
        encounter = manifest
        capture = (manifest_path.parent / manifest["physical_capture"]["relative_path"]).resolve()
    else:
        capture = manifest_path.parent
        ids = manifest.get("encounter_ids", [])
        if encounter_id is None and len(ids) != 1:
            raise ValueError("Choose --encounter-id; capture contains: " + ", ".join(ids))
        selected = encounter_id or ids[0]
        if selected not in ids:
            raise ValueError("Encounter is not in this physical capture")
        candidates = list((capture.parent.parent / "encounters").glob("*/manifest.json"))
        matches = [read_json(p) for p in candidates if read_json(p).get("encounter_id") == selected]
        if len(matches) != 1:
            raise ValueError("Cannot uniquely locate encounter manifest")
        encounter = matches[0]
    if encounter_id and encounter_id != encounter.get("encounter_id"):
        raise ValueError("Encounter ID mismatch")
    if not (capture / "adsb_sbs.log").exists():
        raise ValueError("Standalone adsb_sbs.log required; FULL-reference captures are not yet supported")
    return capture, encounter


def timed_lines(path):
    path = Path(path)
    index = path.with_name(path.name + ".timing.jsonl")
    with path.open("rb") as stream:
        if not index.exists():
            raise ValueError("RAW receipt timing sidecar required")
        for line in index.read_text().splitlines():
            row = json.loads(line)
            if row["offset"] < 0 or row["length"] <= 0:
                raise ValueError("Invalid timing sidecar span")
            stream.seek(row["offset"])
            data = stream.read(row["length"])
            if len(data) != row["length"]:
                raise ValueError("Truncated recorder stream")
            yield timestamp(row["received_at_utc"]), data.decode().strip()


def load_local(capture, icao, timezone, max_gap):
    from raw_adsb_track import decode_raw_tc19_track, decode_raw_tc19_altitude, decode_raw_tc31_version
    values = {key: [] for key in (*FIELDS, "sbs_track_deg")}
    rejected = 0
    for line in (capture / "adsb_sbs.log").read_text().splitlines():
        p = line.split(",")
        if len(p) < 17 or p[4].upper() != icao.upper():
            continue
        t = timestamp(p[6] + " " + p[7], timezone)
        def add(key, text):
            nonlocal rejected
            if not text.strip():
                return
            try:
                value = float(text)
                if not math.isfinite(value): raise ValueError()
            except ValueError:
                rejected += 1
                values[key].append((t, None))
                return
            values[key].append((t, value))
        if p[1] == "3":
            add("lat", p[14]); add("lon", p[15])
        if p[1] in ("3", "5"):
            add("baro_ft", p[11])
        if p[1] == "4":
            add("gs_kt", p[12]); add("sbs_track_deg", p[13])
            # SBS vertical-rate source is not specified by the SBS format.
            # Keep it separate; never silently label it geometric/barometric.
            values.setdefault("sbs_rate_fpm", [])
            add("sbs_rate_fpm", p[16])
    raw = capture / "raw_adsb.log"
    versions, deltas = [], []
    if raw.exists():
        for t, line in timed_lines(raw):
            track = decode_raw_tc19_track(line)
            if track and track.icao == icao:
                values["track_deg"].append((t, track.track_deg))
            version = decode_raw_tc31_version(line)
            if version and version.icao == icao:
                versions.append((t, version.adsb_version))
            altitude = decode_raw_tc19_altitude(line)
            if altitude and altitude.icao == icao:
                deltas.append((t, altitude.gnss_minus_baro_ft))
                # Decode the TC19 rate using its recorded source bit.
                from raw_adsb_track import extract_modes_hex
                bits = int(extract_modes_hex(line), 16)
                code = (bits >> (112-78)) & 511
                rate = (code-1)*64 * (-1 if (bits >> (112-69)) & 1 else 1) if code else None
                key = "baro_rate_fpm" if altitude.vertical_rate_source == "BAROMETRIC" else "geom_rate_fpm"
                values[key].append((t, rate))
    baro = Series(values["baro_ft"])
    versions.sort()
    for t, delta in deltas:
        prior = [v for when, v in versions if when <= t]
        b = baro.at(t, max_gap)
        # ADS-B v2 explicitly identifies WGS84 HAE; older/unknown datum is not guessed.
        values["geom_ft"].append((t, b+delta if prior and prior[-1] == 2 and b is not None and delta is not None else None))
    return {key: Series(points) for key, points in values.items()}, rejected


def horizontal_m(a, b):
    if any(x is None for x in (*a, *b)):
        return None
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(circular_difference(a[1], b[1]))
    h = math.sin((lat1-lat2)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dl/2)**2
    return 6371008.8 * 2 * math.asin(math.sqrt(min(1, max(0, h))))


def compare(t, local, reference, kind="observed"):
    row = {"utc": utc(t), "kind": kind}
    for key in FIELDS:
        a, b = local.get(key), reference.get(key)
        row["local_"+key], row["adsblol_"+key] = a, b
        row["delta_"+key] = (None if a is None or b is None else
            circular_difference(a, b) if key in ("track_deg", "lon") else a-b)
    row["horizontal_m"] = horizontal_m((local.get("lat"), local.get("lon")), (reference.get("lat"), reference.get("lon")))
    row["local_sbs_track_deg"] = local.get("sbs_track_deg")
    row["delta_sbs_track_deg"] = (circular_difference(local["sbs_track_deg"], reference["track_deg"])
        if local.get("sbs_track_deg") is not None and reference.get("track_deg") is not None else None)
    row["local_sbs_rate_fpm"] = local.get("sbs_rate_fpm")
    return row


def metrics(rows):
    result = {}
    for key in ("horizontal_m", "delta_baro_ft", "delta_gs_kt", "delta_track_deg", "delta_sbs_track_deg"):
        values = sorted(abs(r[key]) for r in rows if r[key] is not None)
        def percentile(p):
            if not values: return None
            x = (len(values)-1)*p; i = int(x)
            return values[i] + (values[min(i+1, len(values)-1)]-values[i])*(x-i)
        result[key] = {"sample_count": len(values), "median_abs": percentile(.5),
                       "p95_abs": percentile(.95), "max_abs": max(values) if values else None}
    return result


def validate(capture, trace, encounter_id=None, timezone=None, window=60, max_gap=15, prediction_key="latest_prediction", geoid=None):
    if window <= 0 or max_gap <= 0 or not math.isfinite(window+max_gap):
        raise ValueError("Window and interpolation gap must be positive finite seconds")
    folder, event = resolve_capture(capture, encounter_id)
    pred = event[prediction_key]
    if pred.get("prediction_geometry") != "TRUE_2D":
        raise ValueError("This adapter requires an authoritative TRUE_2D prediction")
    t0 = timestamp(pred["predicted_transit_utc"])
    reference = load_trace(trace, event["icao"])
    local, rejected = load_local(folder, event["icao"], timezone, max_gap)
    times = [t for t, v in local["lat"].points if v is not None and abs(t-t0) <= window]
    rows = [compare(t, sample(local,t,max_gap), sample(reference,t,max_gap)) for t in times]
    observed_t0 = compare(t0, sample(local,t0,max_gap), sample(reference,t0,max_gap), "observed_at_t0")
    aircraft = pred.get("aircraft", {})
    final_m = number(pred.get("frozen_vertical_state", {}).get("final_altitude_m"))
    position = {"lat": number(aircraft.get("latitude_deg")), "lon": number(aircraft.get("longitude_deg"))}
    final_hae_ft = None
    if geoid is not None and final_m is not None and None not in position.values():
        final_hae_ft = (final_m + geoid.undulation_m(position["lat"], position["lon"])) / .3048
    predicted = compare(t0, {**position, "geom_ft": final_hae_ft}, sample(reference,t0,max_gap), "predicted_at_t0")
    predicted["local_final_orthometric_m"] = final_m
    predicted["final_altitude_status"] = "EGM96_CONVERTED_TO_WGS84_HAE" if final_hae_ft is not None else "UNAVAILABLE_DATUM_CONVERSION"
    report = {"schema_version": 1, "icao": event["icao"], "callsign": event.get("callsign"),
        "encounter_id": event["encounter_id"], "body": event["body"], "prediction_selection": prediction_key,
        "prediction_base_utc": pred.get("frozen_vertical_state", {}).get("prediction_base_utc"),
        "predicted_t0_utc": utc(t0), "window_seconds": window, "max_interpolation_gap_seconds": max_gap,
        "sbs_timezone": timezone, "metrics": metrics(rows), "observed_at_t0": observed_t0,
        "predicted_at_t0": predicted, "rows": rows, "rejected_local_numeric_fields": rejected,
        "semantics": {"delta": "local minus ADSB.lol; angular deltas shortest signed arc",
          "metrics": "absolute errors at local position update timestamps; per-field valid counts",
          "horizontal": "spherical great-circle distance, radius 6371008.8 m; diagnostic only",
          "local_time": "SBS generated timestamps; RAW track/rate/delta use recorded receipt UTC",
          "local_track": "RAW TC19 only; SBS retained separately, no implicit source fallback",
          "interpolation": "linear; shortest arc longitude/track; bounded gaps; no extrapolation",
          "altitude": "baro_ft uncorrected pressure altitude; geom_ft WGS84 HAE",
          "local_geom": "ADS-B v2 TC19 GNSS-minus-baro plus time-interpolated SBS baro; unavailable without v2",
          "missing": "JSON null / blank CSV; no inferred values; no observer data exported"}}
    return report, local, reference


def write_report(report, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    rows = [*report["rows"], report["observed_at_t0"], report["predicted_at_t0"]]
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with (output / "comparison.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader(); writer.writerows(rows)
