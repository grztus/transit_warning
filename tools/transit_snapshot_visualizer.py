"""Offline schema-v3 transit snapshot trajectory visualizer."""

from __future__ import annotations

import argparse
import datetime
import json
import math
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import ephem

from transit_prediction_model import (
    IntentParameter,
    MotionParameter,
    VerticalIntentState,
    VerticalMotionState,
    VerticalPredictionPolicy,
    angular_position_from_observer,
    horizontal_position_from_t0,
    predict_vertical_state_at_time,
)


UTC = datetime.timezone.utc
HORIZONTAL_TOLERANCE_DEG = 1e-9
BODY_TOLERANCE_DEG = 1e-6
ALTITUDE_TOLERANCE_M = 1e-9
ANGULAR_TOLERANCE_DEG = 1e-9


class VisualizerError(ValueError):
    """A clear snapshot, reconstruction, or CLI validation failure."""


@dataclass(frozen=True)
class BodyPosition:
    altitude_deg: float
    azimuth_deg: float


@dataclass(frozen=True)
class TrajectorySample:
    offset_seconds: float
    timestamp_utc: datetime.datetime
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    aircraft_azimuth_deg: float
    aircraft_altitude_deg: float
    body_azimuth_deg: float
    body_altitude_deg: float
    x_deg: float
    y_deg: float
    center_separation_deg: float


@dataclass(frozen=True)
class VisualizationResult:
    output_path: Path
    prediction_label: str
    production_separation_deg: float
    body_radius_deg: float
    minimum_separation_deg: float
    minimum_ratio: float
    closest_offset_seconds: float
    closest_utc: datetime.datetime
    disk_crossing: bool
    sample_count: int


def parse_utc(value):
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise VisualizerError("invalid UTC timestamp: {!r}".format(value)) from error
    if parsed.tzinfo is None or parsed.utcoffset() != datetime.timedelta(0):
        raise VisualizerError("timestamp must be timezone-aware UTC: {!r}".format(value))
    return parsed.astimezone(UTC)


def load_snapshot(path):
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VisualizerError("cannot read snapshot: {}".format(error)) from error
    if document.get("schema_version", 0) < 3:
        raise VisualizerError("schema_version >= 3 is required")
    return document


def select_prediction(document, selector="final"):
    updates = document.get("prediction_updates") or []
    if selector == "trigger":
        return document["trigger_prediction"], "trigger"
    if selector == "final":
        return (updates[-1] if updates else document["trigger_prediction"],
                "final")
    if selector.startswith("update:"):
        try:
            index = int(selector.split(":", 1)[1])
            prediction = updates[index]
        except (ValueError, IndexError) as error:
            raise VisualizerError(
                "prediction update index is invalid: {}".format(selector)) from error
        return prediction, "update:{}".format(index)
    raise VisualizerError("prediction must be trigger, final, or update:N")


def sample_offsets(before, after, step):
    before, after, step = float(before), float(after), float(step)
    if before < 0 or after < 0:
        raise VisualizerError("before and after must be non-negative")
    if not math.isfinite(step) or step <= 0:
        raise VisualizerError("step must be a positive finite number")
    if not math.isfinite(before) or not math.isfinite(after):
        raise VisualizerError("before and after must be finite")
    count = int(math.floor((before + after) / step))
    values = [-before + index * step for index in range(count + 1)]
    if not values or values[-1] < after - 1e-12:
        values.append(after)
    values.append(0.0)
    return tuple(sorted(set(0.0 if abs(value) < 1e-12 else value
                            for value in values)))


def tangent_plane_offset(aircraft_azimuth_deg, aircraft_altitude_deg,
                         body_azimuth_deg, body_altitude_deg):
    """Spherical logarithmic-map projection: +X azimuth, +Y altitude."""
    az = math.radians(float(aircraft_azimuth_deg))
    alt = math.radians(float(aircraft_altitude_deg))
    center_az = math.radians(float(body_azimuth_deg))
    center_alt = math.radians(float(body_altitude_deg))

    target = (math.cos(alt) * math.sin(az),
              math.cos(alt) * math.cos(az), math.sin(alt))
    center = (math.cos(center_alt) * math.sin(center_az),
              math.cos(center_alt) * math.cos(center_az),
              math.sin(center_alt))
    right = (math.cos(center_az), -math.sin(center_az), 0.0)
    up = (-math.sin(center_alt) * math.sin(center_az),
          -math.sin(center_alt) * math.cos(center_az),
          math.cos(center_alt))
    dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(target, center))))
    separation = math.acos(dot)
    if separation == 0:
        return 0.0, 0.0
    sine = math.sin(separation)
    if abs(sine) < 1e-15:
        raise VisualizerError("tangent-plane projection is undefined at antipode")
    scale = separation / sine
    x = scale * sum(a * b for a, b in zip(target, right))
    y = scale * sum(a * b for a, b in zip(target, up))
    return math.degrees(x), math.degrees(y)


def body_position_at_utc(body_name, when_utc, observer):
    if when_utc.tzinfo is None or when_utc.utcoffset() != datetime.timedelta(0):
        raise VisualizerError("body ephemeris requires timezone-aware UTC")
    ephem_observer = ephem.Observer()
    ephem_observer.lat = str(observer["lat"])
    ephem_observer.lon = str(observer["lon"])
    ephem_observer.elevation = float(observer["elevation_m"])
    ephem_observer.date = ephem.Date(when_utc)
    if body_name.upper() == "SUN":
        body = ephem.Sun(ephem_observer)
    elif body_name.upper() == "MOON":
        body = ephem.Moon(ephem_observer)
    else:
        raise VisualizerError("unsupported celestial body: {}".format(body_name))
    body.compute(ephem_observer)
    return BodyPosition(math.degrees(body.alt), math.degrees(body.az))


def _motion_parameter(item, value_key):
    if item is None:
        return None
    return MotionParameter(float(item[value_key]), parse_utc(item["timestamp_utc"]),
                           item["source"])


def _intent_parameter(item, value_key):
    if item is None:
        return None
    return IntentParameter(float(item[value_key]), parse_utc(item["timestamp_utc"]),
                           item["source"])


def vertical_inputs(prediction):
    vertical = prediction["frozen_prediction_state"]["vertical"]
    altitude = _motion_parameter(vertical.get("current_altitude"), "value_m")
    motion = VerticalMotionState(
        altitude=altitude,
        vertical_rate=_motion_parameter(
            vertical.get("latest_vertical_rate"), "value_fpm"),
        vertical_rate_history=tuple(_motion_parameter(item, "value_fpm")
                                    for item in vertical.get(
                                        "vertical_rate_history", [])),
    )
    intent = VerticalIntentState(
        selected_altitude=_intent_parameter(
            vertical.get("selected_altitude"), "value_ft"),
        nav_qnh=_intent_parameter(vertical.get("nav_qnh"), "value_hpa"),
    )
    return (vertical, altitude, motion, intent,
            VerticalPredictionPolicy(**vertical["policy"]))


def check_provider_version(prediction):
    astronomy = prediction["frozen_prediction_state"].get("astronomy", {})
    provider = astronomy.get("provider")
    version = astronomy.get("provider_version")
    current = str(getattr(ephem, "__version__", getattr(ephem, "version", "unknown")))
    if provider and provider != "PyEphem":
        warnings.warn("snapshot astronomy provider is {}, expected PyEphem".format(provider))
    if version and str(version) != current:
        warnings.warn("PyEphem version mismatch: snapshot {}, current {}".format(
            version, current))


def reconstruct_samples(document, prediction, before=15.0, after=15.0,
                        step=0.1):
    observer = document.get("observer") or prediction["observer"]
    body_name = prediction["body"]
    t0 = parse_utc(prediction["predicted_transit_utc"])
    time2x = float(prediction["time2x_seconds"])
    horizontal = prediction["frozen_prediction_state"]["horizontal"]
    intersection = prediction["intersection"]
    vertical, altitude, motion, intent, policy = vertical_inputs(prediction)
    base = parse_utc(vertical["evaluated_at_utc"])
    samples = []
    for offset in sample_offsets(before, after, step):
        sample_utc = t0 + datetime.timedelta(seconds=offset)
        latitude, longitude = horizontal_position_from_t0(
            intersection["lat"], intersection["lon"],
            horizontal["forward_bearing_at_t0_deg"],
            horizontal["effective_groundspeed_kmh"], offset,
            horizontal.get("earth_radius_km", 6371.0))
        vertical_state = predict_vertical_state_at_time(
            altitude.value, motion, intent, base, time2x + offset,
            vertical["application_qnh_hpa"], policy)
        angular = angular_position_from_observer(
            (float(observer["lat"]), float(observer["lon"])),
            float(observer["elevation_m"]), (latitude, longitude),
            vertical_state.prediction.predicted_altitude_m)
        body = body_position_at_utc(body_name, sample_utc, observer)
        x, y = tangent_plane_offset(
            angular.azimuth_deg, angular.altitude_angle_deg,
            body.azimuth_deg, body.altitude_deg)
        samples.append(TrajectorySample(
            offset, sample_utc, latitude, longitude,
            vertical_state.prediction.predicted_altitude_m,
            angular.azimuth_deg, angular.altitude_angle_deg,
            body.azimuth_deg, body.altitude_deg, x, y,
            math.hypot(x, y)))
    return tuple(samples)


def validate_t0(document, prediction, samples):
    t0_sample = next(sample for sample in samples if sample.offset_seconds == 0.0)
    intersection = prediction["intersection"]
    vertical = prediction["frozen_prediction_state"]["vertical"]
    decision = vertical["decision"]
    failures = []

    def check(name, actual, expected, tolerance):
        if abs(float(actual) - float(expected)) > tolerance:
            failures.append("{}: reconstructed={!r}, stored={!r}, tolerance={}".format(
                name, actual, expected, tolerance))

    check("intersection latitude", t0_sample.latitude_deg,
          intersection["lat"], HORIZONTAL_TOLERANCE_DEG)
    check("intersection longitude", t0_sample.longitude_deg,
          intersection["lon"], HORIZONTAL_TOLERANCE_DEG)
    check("body altitude", t0_sample.body_altitude_deg,
          intersection["body_altitude_deg"], BODY_TOLERANCE_DEG)
    check("body azimuth", t0_sample.body_azimuth_deg,
          intersection["body_azimuth_deg"], BODY_TOLERANCE_DEG)
    check("aircraft altitude", t0_sample.altitude_m,
          decision["predicted_altitude_m"], ALTITUDE_TOLERANCE_M)
    check("aircraft azimuth", t0_sample.aircraft_azimuth_deg,
          intersection["azimuth_from_observer_deg"], ANGULAR_TOLERANCE_DEG)
    check("aircraft altitude angle", t0_sample.aircraft_altitude_deg,
          intersection["aircraft_altitude_deg"], ANGULAR_TOLERANCE_DEG)
    check("production separation",
          abs(t0_sample.aircraft_altitude_deg - t0_sample.body_altitude_deg),
          prediction["separation_deg"], ANGULAR_TOLERANCE_DEG)
    expected_t0 = parse_utc(prediction["prediction_base_utc"]) + datetime.timedelta(
        seconds=float(prediction["time2x_seconds"]))
    if abs((t0_sample.timestamp_utc - expected_t0).total_seconds()) > 1e-6:
        failures.append("predicted transit UTC disagrees with base + time2X")
    if failures:
        raise VisualizerError("T0 validation failed:\n  " + "\n  ".join(failures))
    return t0_sample


def body_radius_deg(prediction):
    diameter = float(prediction["body_angular_diameter_arcsec"])
    if not math.isfinite(diameter) or diameter <= 0:
        raise VisualizerError("body angular diameter must be positive")
    return diameter / 7200.0


def trajectory_summary(samples, radius_deg):
    closest = min(samples, key=lambda item: item.center_separation_deg)
    return closest, closest.center_separation_deg / radius_deg, any(
        sample.center_separation_deg <= radius_deg for sample in samples)


def render_png(document, prediction, prediction_label, samples, output_path):
    import matplotlib
    matplotlib.use("Agg", force=True)
    from matplotlib import pyplot as plt

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    radius = body_radius_deg(prediction)
    closest, ratio, crossing = trajectory_summary(samples, radius)
    t0_sample = next(sample for sample in samples if sample.offset_seconds == 0.0)
    figure, axis = plt.subplots(figsize=(8, 8))
    axis.add_patch(plt.Circle((0, 0), radius, color="#f5c542", alpha=0.35,
                              ec="#d89200", lw=2, label=prediction["body"]))
    axis.scatter([0], [0], marker="+", color="black", s=80, zorder=4)
    xs, ys = [sample.x_deg for sample in samples], [sample.y_deg for sample in samples]
    axis.plot(xs, ys, color="#1769aa", lw=1.8, label="predicted aircraft path")
    axis.scatter([t0_sample.x_deg], [t0_sample.y_deg], color="red", s=45,
                 zorder=5, label="T0")
    marker_stride = max(1, len(samples) // 10)
    for sample in samples[::marker_stride]:
        axis.scatter([sample.x_deg], [sample.y_deg], color="#1769aa", s=10)
        axis.annotate("{:+.1f}s".format(sample.offset_seconds),
                      (sample.x_deg, sample.y_deg), fontsize=7,
                      xytext=(3, 3), textcoords="offset points")
    if len(samples) >= 2:
        tail, head = samples[-2], samples[-1]
        axis.annotate("", xy=(head.x_deg, head.y_deg),
                      xytext=(tail.x_deg, tail.y_deg),
                      arrowprops={"arrowstyle": "->", "color": "#1769aa"})
    aircraft = document.get("aircraft", {})
    metadata = (
        "{} / {} | {} | {}\n"
        "Body alt: {:.3f}° | Production SEP (vertical): {:.4f}°\n"
        "Sampled minimum center separation: {:.4f}° ({:.3f} radius)\n"
        "Body radius: {:.4f}° | closest: {:+.2f}s | inside disk: {}"
    ).format(aircraft.get("callsign") or "NOCALL", aircraft.get("icao") or "?",
             prediction["body"], prediction_label,
             float(prediction["body_altitude_deg"]),
             float(prediction["separation_deg"]),
             closest.center_separation_deg, ratio, radius,
             closest.offset_seconds, "YES" if crossing else "NO")
    axis.set_title(metadata, fontsize=10)
    axis.set_xlabel("X: increasing azimuth / visual right (degrees)")
    axis.set_ylabel("Y: increasing altitude / visual up (degrees)")
    axis.set_aspect("equal", adjustable="datalim")
    axis.grid(True, alpha=0.35)
    axis.legend(loc="best", fontsize=8)
    figure.tight_layout()
    figure.savefig(output_path, dpi=150)
    plt.close(figure)
    return VisualizationResult(
        output_path, prediction_label, float(prediction["separation_deg"]),
        radius, closest.center_separation_deg, ratio,
        closest.offset_seconds, closest.timestamp_utc, crossing, len(samples))


def visualize(snapshot_path, output_path, prediction_selector="final",
              before=15.0, after=15.0, step=0.1):
    document = load_snapshot(snapshot_path)
    prediction, label = select_prediction(document, prediction_selector)
    check_provider_version(prediction)
    samples = reconstruct_samples(document, prediction, before, after, step)
    validate_t0(document, prediction, samples)
    return render_png(document, prediction, label, samples, output_path)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Render a schema-v3 Transit Warning snapshot offline")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--prediction", default="final")
    parser.add_argument("--before", type=float, default=15.0)
    parser.add_argument("--after", type=float, default=15.0)
    parser.add_argument("--step", type=float, default=0.1)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = visualize(args.snapshot, args.output, args.prediction,
                           args.before, args.after, args.step)
    except VisualizerError as error:
        parser.error(str(error))
    print("PNG: {}".format(result.output_path))
    print("Production SEP: {:.6f} deg".format(result.production_separation_deg))
    print("Sampled minimum: {:.6f} deg ({:.3f} radius) at {:+.3f} s".format(
        result.minimum_separation_deg, result.minimum_ratio,
        result.closest_offset_seconds))
    print("Disk crossing: {}".format("YES" if result.disk_crossing else "NO"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
