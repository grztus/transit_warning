"""Opt-in ADSB.lol diagnostics only: no predictions, candidates or map updates."""

import argparse
import json
import math
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.adsblol_live import AdsbLolProvider, MIN_POLL_SECONDS


def console_summary(report):
    print(f"ADSBLOL {report['status']} | aircraft={report['aircraft_count']} | HTTP={report['http_status']}")
    print(f"Request UTC: {report['request_started_utc']} -> {report['request_finished_utc']}")
    print(f"HTTP latency: {report['response_latency_seconds']} s (not field freshness)")
    print(f"Provider snapshot UTC: {report.get('provider_snapshot_at_utc')}"
          f" | processing: {report.get('provider_processing_ms')} ms")
    if report["error"]:
        print(report["error"])
    for aircraft in report["aircraft"]:
        print(f"Aircraft {aircraft['aircraft_address']}:")
        for name, field in aircraft["fields"].items():
            value = json.dumps(field["value"], ensure_ascii=True, allow_nan=False)
            print(f"  {name}: {value} {field['unit'] or ''} [{field['datum_or_reference']}]"
                  f" | {field['availability']} freshness={field['freshness_state']}"
                  f" age={field['age_seconds']} {field['age_reference'] or ''}"
                  f" | quality={field['quality']}")
        if aircraft["warnings"]:
            print("  Timing warnings: " + ", ".join(aircraft["warnings"]))
    print("UNKNOWN means measurement age is unavailable, not fresh. No predictor integration.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observer-mode", choices=("STATIC", "MANUAL", "MOBILE"), default="STATIC",
                        help="Explicit query authorization; MOBILE always refuses acquisition.")
    parser.add_argument("--lat", type=float, help="Explicitly authorized query latitude (sent to ADSB.lol)")
    parser.add_argument("--lon", type=float, help="Explicitly authorized query longitude (sent to ADSB.lol)")
    parser.add_argument("--radius-nm", type=int, default=100, help="Integer NM radius, capped at 250")
    parser.add_argument("--icao", help="Single ICAO lookup instead of geographic query")
    parser.add_argument("--timeout-seconds", type=float, default=10)
    parser.add_argument("--retries", type=int, default=1, help="Bounded transient retries, 0..3")
    parser.add_argument("--poll-seconds", type=float, default=MIN_POLL_SECONDS,
                        help="Diagnostic pacing, at least 10 seconds; not a freshness threshold")
    parser.add_argument("--count", type=int, default=1, help="Finite acquisition count; default one-shot")
    parser.add_argument("--json-output", type=Path, help="New private JSON report file; never overwritten")
    args = parser.parse_args(argv)
    if args.count < 1 or not math.isfinite(args.poll_seconds) or args.poll_seconds < MIN_POLL_SECONDS:
        parser.error("Count must be positive and poll interval must be finite and at least 10 seconds.")
    cancel = threading.Event()
    try:
        provider = AdsbLolProvider(timeout_seconds=args.timeout_seconds, max_retries=args.retries,
                                   cancel_event=cancel)
    except ValueError as error:
        parser.error(str(error))
    reports = []
    output = None
    try:
        # Exclusive creation checks output intent before any acquisition.
        if args.json_output:
            output = args.json_output.open("x", encoding="utf-8")
        for index in range(args.count):
            report = provider.acquire(observer_mode=args.observer_mode, lat=args.lat, lon=args.lon,
                                      radius_nm=args.radius_nm, icao=args.icao)
            reports.append(report)
            console_summary(report)
            if report["status"] in ("PRIVACY_BLOCKED", "INVALID_QUERY", "AUTH_ERROR", "TLS_ERROR",
                                    "MALFORMED_RESPONSE", "CANCELLED"):
                break
            if report["status"] == "HTTP_ERROR" and not report["retry_after_seconds"]:
                break
            if index + 1 < args.count:
                remaining = max(args.poll_seconds, report["retry_after_seconds"] or 0)
                while remaining > 0 and not cancel.is_set():
                    step = min(remaining, 60)
                    cancel.wait(step)
                    remaining -= step
    except KeyboardInterrupt:
        cancel.set()
        print("Acquisition cancelled.")
        return 130
    except OSError:
        parser.exit(2, "Cannot create/write the private diagnostic output file. Existing files are not overwritten.\n")
    finally:
        if output:
            try:
                json.dump({"schema_version": 1, "diagnostic_only": True, "responses": reports},
                          output, indent=2, allow_nan=False)
                output.write("\n")
            except OSError:
                parser.exit(2, "Cannot write the private diagnostic output file.\n")
            finally:
                output.close()
    return 0 if reports and all(r["status"] == "OK" for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
