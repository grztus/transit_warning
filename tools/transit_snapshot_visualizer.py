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
DEFAULT_EDGE_TOLERANCE_RADII = 0.05


class VisualizerError(ValueError):
    """A clear snapshot, reconstruction, or CLI validation failure."""


@dataclass(frozen=True)
class BodyPosition:
    altitude_deg: float
    azimuth_deg: float


@dataclass(frozen=True)
class DiagnosticAngularPosition:
    distance_km: float
    azimuth_deg: float
    altitude_angle_deg: float


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
    high_precision_minimum_separation_deg: float | None = None
    high_precision_minimum_ratio: float | None = None
    high_precision_closest_offset_seconds: float | None = None
    high_precision_closest_utc: datetime.datetime | None = None
    high_precision_disk_crossing: bool | None = None
    high_precision_delta_deg: float | None = None
    high_precision_delta_body_radii: float | None = None
    classification: str | None = None
    production_minimum_separation_deg: float | None = None
    production_minimum_ratio: float | None = None
    production_closest_offset_seconds: float | None = None
    production_closest_utc: datetime.datetime | None = None
    production_disk_crossing: bool | None = None


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


def high_precision_angular_position(observer_position, observer_elevation_m,
                                    target_position, target_altitude_m):
    """Diagnostic observer geometry without production display rounding."""
    observer_lat, observer_lon = map(math.radians, observer_position)
    target_lat, target_lon = map(math.radians, target_position)
    delta_latitude = target_lat - observer_lat
    delta_longitude = target_lon - observer_lon
    haversine = (
        math.sin(delta_latitude / 2.0) ** 2
        + math.cos(observer_lat) * math.cos(target_lat)
        * math.sin(delta_longitude / 2.0) ** 2)
    distance_km = 2.0 * 6371.0 * math.atan2(
        math.sqrt(haversine), math.sqrt(1.0 - haversine))
    azimuth = math.atan2(
        math.sin(delta_longitude) * math.cos(target_lat),
        math.cos(observer_lat) * math.sin(target_lat)
        - math.sin(observer_lat) * math.cos(target_lat)
        * math.cos(delta_longitude))
    altitude_angle = math.degrees(math.atan(
        (float(target_altitude_m) - float(observer_elevation_m))
        / (distance_km * 1000.0)))
    return DiagnosticAngularPosition(
        distance_km=distance_km,
        azimuth_deg=(math.degrees(azimuth) + 360.0) % 360.0,
        altitude_angle_deg=altitude_angle,
    )


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
    if item is None or item.get(value_key) is None:
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
    altitude_selection = prediction.get("geometric_altitude_selection") or {}
    geometric_correction_m = float(
        altitude_selection.get("correction_m") or 0.0)
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
        selected_altitude_m = (
            vertical_state.prediction.predicted_altitude_m
            + geometric_correction_m)
        angular = angular_position_from_observer(
            (float(observer["lat"]), float(observer["lon"])),
            float(observer["elevation_m"]), (latitude, longitude),
            selected_altitude_m)
        body = body_position_at_utc(body_name, sample_utc, observer)
        x, y = tangent_plane_offset(
            angular.azimuth_deg, angular.altitude_angle_deg,
            body.azimuth_deg, body.altitude_deg)
        samples.append(TrajectorySample(
            offset, sample_utc, latitude, longitude,
            selected_altitude_m,
            angular.azimuth_deg, angular.altitude_angle_deg,
            body.azimuth_deg, body.altitude_deg, x, y,
            math.hypot(x, y)))
    return tuple(samples)


def reconstruct_high_precision_samples(document, production_samples):
    """Reproject identical sampled 3D states without production rounding."""
    observer = document["observer"]
    observer_position = (float(observer["lat"]), float(observer["lon"]))
    observer_elevation = float(observer["elevation_m"])
    samples = []
    for sample in production_samples:
        angular = high_precision_angular_position(
            observer_position, observer_elevation,
            (sample.latitude_deg, sample.longitude_deg), sample.altitude_m)
        x, y = tangent_plane_offset(
            angular.azimuth_deg, angular.altitude_angle_deg,
            sample.body_azimuth_deg, sample.body_altitude_deg)
        samples.append(TrajectorySample(
            sample.offset_seconds, sample.timestamp_utc,
            sample.latitude_deg, sample.longitude_deg, sample.altitude_m,
            angular.azimuth_deg, angular.altitude_angle_deg,
            sample.body_azimuth_deg, sample.body_altitude_deg,
            x, y, math.hypot(x, y)))
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
    altitude_selection = prediction.get("geometric_altitude_selection") or {}
    expected_altitude_m = altitude_selection.get(
        "selected_altitude_m", decision["predicted_altitude_m"])
    check("aircraft altitude", t0_sample.altitude_m,
          expected_altitude_m, ALTITUDE_TOLERANCE_M)
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


def disk_classification(ratio, edge_tolerance_radii=DEFAULT_EDGE_TOLERANCE_RADII):
    """Classify a sampled 2D closest approach for diagnostics only."""
    ratio = float(ratio)
    tolerance = float(edge_tolerance_radii)
    if not math.isfinite(tolerance) or tolerance < 0:
        raise VisualizerError("edge tolerance must be a finite non-negative number")
    if ratio < 1.0 - tolerance:
        return "HIT"
    if ratio <= 1.0 + tolerance:
        return "EDGE"
    return "MISS"


def zoom_plot_limits(radius_deg, zoom):
    """Return symmetric body-centered limits for a plotting-only zoom."""
    if zoom is None:
        return None
    zoom = float(zoom)
    if not math.isfinite(zoom) or zoom < 1.0:
        raise VisualizerError("zoom must be a finite number greater than or equal to 1.0")
    extent = zoom * float(radius_deg)
    return (-extent, extent), (-extent, extent)


def render_png(document, prediction, prediction_label, samples, output_path,
               zoom=None, production_samples=None,
               edge_tolerance_radii=DEFAULT_EDGE_TOLERANCE_RADII):
    import matplotlib
    matplotlib.use("Agg", force=True)
    from matplotlib import pyplot as plt

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    radius = body_radius_deg(prediction)
    zoom_limits = zoom_plot_limits(radius, zoom)
    closest, ratio, crossing = trajectory_summary(samples, radius)
    classification = disk_classification(ratio, edge_tolerance_radii)
    production_summary = (
        trajectory_summary(production_samples, radius)
        if production_samples is not None else None)
    t0_sample = next(sample for sample in samples if sample.offset_seconds == 0.0)
    figure, axis = plt.subplots(figsize=(8, 8))
    axis.add_patch(plt.Circle((0, 0), radius, color="#f5c542", alpha=0.35,
                              ec="#d89200", lw=2, label=prediction["body"]))
    axis.scatter([0], [0], marker="+", color="black", s=80, zorder=4)
    xs, ys = [sample.x_deg for sample in samples], [sample.y_deg for sample in samples]
    axis.plot(xs, ys, color="#1769aa", lw=2.0,
              label="smooth diagnostic path")
    if production_samples is not None:
        production_xs = [sample.x_deg for sample in production_samples]
        production_ys = [sample.y_deg for sample in production_samples]
        axis.plot(production_xs, production_ys, color="#d1495b", lw=1.2,
                  ls="--", alpha=0.85, label="production-quantized path")
    axis.scatter([t0_sample.x_deg], [t0_sample.y_deg], color="red", s=45,
                 zorder=5, label="production T0")
    axis.scatter([closest.x_deg], [closest.y_deg], facecolors="none",
                 edgecolors="#2a7f2e", marker="o", s=80, linewidths=1.5,
                 zorder=5, label="sampled 2D closest approach")
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
    zoom_metadata = (
        "\nZoom: ±{:.1f} body radii".format(float(zoom))
        if zoom is not None else "")
    metadata = (
        "{} / {} | {} | {}\n"
        "Production SEP (vertical): {:.4f}° | Body radius: {:.4f}°\n"
        "High-precision 2D minimum: {:.4f}° ({:.3f} R) at {:+.2f}s\n"
        "2D disk result: {} | edge tolerance: ±{:.3f} R{}"
    ).format(aircraft.get("callsign") or "NOCALL", aircraft.get("icao") or "?",
             prediction["body"], prediction_label,
             float(prediction["separation_deg"]), radius,
             closest.center_separation_deg, ratio, closest.offset_seconds,
             classification, float(edge_tolerance_radii), zoom_metadata)
    axis.set_title(metadata, fontsize=10)
    axis.set_xlabel("X: increasing azimuth / visual right (degrees)")
    axis.set_ylabel("Y: increasing altitude / visual up (degrees)")
    if zoom_limits is None:
        axis.set_aspect("equal", adjustable="datalim")
    else:
        axis.set_xlim(*zoom_limits[0])
        axis.set_ylim(*zoom_limits[1])
        axis.set_aspect("equal", adjustable="box")
    axis.grid(True, alpha=0.35)
    axis.legend(loc="best", fontsize=8)
    figure.tight_layout()
    figure.savefig(output_path, dpi=150)
    plt.close(figure)
    result = VisualizationResult(
        output_path, prediction_label, float(prediction["separation_deg"]),
        radius, closest.center_separation_deg, ratio,
        closest.offset_seconds, closest.timestamp_utc, crossing, len(samples),
        high_precision_minimum_separation_deg=closest.center_separation_deg,
        high_precision_minimum_ratio=ratio,
        high_precision_closest_offset_seconds=closest.offset_seconds,
        high_precision_closest_utc=closest.timestamp_utc,
        high_precision_disk_crossing=crossing,
        classification=classification)
    if production_summary is None:
        return result
    production_closest, production_ratio, production_crossing = production_summary
    return VisualizationResult(
        **{**result.__dict__,
           "high_precision_delta_deg": (closest.center_separation_deg
                                        - production_closest.center_separation_deg),
           "high_precision_delta_body_radii": ratio - production_ratio,
           "production_minimum_separation_deg": production_closest.center_separation_deg,
           "production_minimum_ratio": production_ratio,
           "production_closest_offset_seconds": production_closest.offset_seconds,
           "production_closest_utc": production_closest.timestamp_utc,
           "production_disk_crossing": production_crossing})


def visualize(snapshot_path, output_path, prediction_selector="final",
              before=15.0, after=15.0, step=0.1, zoom=None,
              high_precision_overlay=False, show_production_path=False,
              edge_tolerance_radii=DEFAULT_EDGE_TOLERANCE_RADII):
    document = load_snapshot(snapshot_path)
    prediction, label = select_prediction(document, prediction_selector)
    check_provider_version(prediction)
    production_samples = reconstruct_samples(
        document, prediction, before, after, step)
    validate_t0(document, prediction, production_samples)
    samples = reconstruct_high_precision_samples(document, production_samples)
    show_production_path = show_production_path or high_precision_overlay
    return render_png(
        document, prediction, label, samples, output_path, zoom=zoom,
        production_samples=(production_samples if show_production_path else None),
        edge_tolerance_radii=edge_tolerance_radii)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Render a schema-v3 Transit Warning snapshot offline")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--prediction", default="final")
    parser.add_argument("--before", type=float, default=15.0)
    parser.add_argument("--after", type=float, default=15.0)
    parser.add_argument("--step", type=float, default=0.1)
    parser.add_argument("--zoom", type=float)
    parser.add_argument("--show-production-path", action="store_true")
    parser.add_argument("--high-precision-overlay", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--edge-tolerance-radii", type=float,
                        default=DEFAULT_EDGE_TOLERANCE_RADII)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = visualize(args.snapshot, args.output, args.prediction,
                           args.before, args.after, args.step, args.zoom,
                           args.high_precision_overlay,
                           args.show_production_path,
                           args.edge_tolerance_radii)
    except VisualizerError as error:
        parser.error(str(error))
    print("PNG: {}".format(result.output_path))
    print("Production SEP: {:.6f} deg".format(result.production_separation_deg))
    print("High-precision 2D minimum: {:.6f} deg ({:.3f} radius) at {:+.3f} s".format(
        result.minimum_separation_deg, result.minimum_ratio,
        result.closest_offset_seconds))
    print("2D disk result: {}".format(result.classification))
    if result.production_minimum_separation_deg is not None:
        print("Production-path minimum: {:.6f} deg ({:.3f} radius) at {:+.3f} s".format(
            result.production_minimum_separation_deg,
            result.production_minimum_ratio,
            result.production_closest_offset_seconds))
        print("High precision - production: {:+.6f} deg ({:+.3f} radius)".format(
            result.high_precision_delta_deg,
            result.high_precision_delta_body_radii))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
