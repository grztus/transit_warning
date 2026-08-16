"""Replay recorded SBS/BaseStation data over the Transit Warning TCP ports."""

from __future__ import annotations

import argparse
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator

from transit_time import port_timestamp_to_utc


ADSB_PORT = 30003
MLAT_PORT = 30106
TIMESTAMP_FORMAT = "%Y/%m/%d %H:%M:%S.%f"


@dataclass(frozen=True)
class Scenario:
    name: str
    active_port: int
    silent_port: int
    default_path: Path


@dataclass(frozen=True)
class DualScenario:
    name: str
    adsb_path: Path
    mlat_path: Path


SCENARIOS = {
    "adsb-2026": Scenario(
        "adsb-2026",
        ADSB_PORT,
        MLAT_PORT,
        Path("tests/data/adsb_30003_20260816_120418.log"),
    ),
    "mlat-2024": Scenario(
        "mlat-2024",
        MLAT_PORT,
        ADSB_PORT,
        Path("tests/data/mlat_2024-05-18.log"),
    ),
}

DUAL_SCENARIOS = {
    "dual-2026": DualScenario(
        "dual-2026",
        Path("tests/data/adsb_30003_20260816_120418.log"),
        Path("tests/data/mlat_synthetic_20260816.log"),
    ),
}


def message_timestamp(line: str) -> datetime:
    """Return the BaseStation generated timestamp (fields 6 and 7)."""
    fields = line.rstrip("\r\n").split(",")
    if len(fields) < 8:
        raise ValueError("message has fewer than 8 fields")
    return datetime.strptime(f"{fields[6].strip()} {fields[7].strip()}", TIMESTAMP_FORMAT)


def logged_timestamp(line: str, port: int) -> datetime:
    """Return Logged fields 8-9 on the port's existing UTC timeline."""
    fields = line.rstrip("\r\n").split(",")
    if len(fields) < 10:
        raise ValueError("message has fewer than 10 fields")
    parsed = datetime.strptime(f"{fields[8].strip()} {fields[9].strip()}", TIMESTAMP_FORMAT)
    return port_timestamp_to_utc(parsed, port)


def merge_logged_streams(
    adsb_lines: Iterable[str],
    mlat_lines: Iterable[str],
) -> Iterator[tuple[datetime, int, str]]:
    """Merge two chronological streams; port 30003 wins equal timestamps.

    Only one pending record per input is retained.
    """
    sources = {ADSB_PORT: iter(adsb_lines), MLAT_PORT: iter(mlat_lines)}
    pending: dict[int, tuple[datetime, str]] = {}
    for port, source in sources.items():
        try:
            line = next(source)
            pending[port] = (logged_timestamp(line, port), line)
        except StopIteration:
            pass

    while pending:
        port = min(pending, key=lambda candidate: (pending[candidate][0], candidate))
        timestamp, line = pending.pop(port)
        yield timestamp, port, line
        try:
            next_line = next(sources[port])
            pending[port] = (logged_timestamp(next_line, port), next_line)
        except StopIteration:
            pass


def replay_dual_streams(
    adsb_lines: Iterable[str],
    mlat_lines: Iterable[str],
    clients: dict[int, socket.socket],
    speed: float | None,
    stop_event: threading.Event,
) -> int:
    """Send both inputs on one globally paced Logged UTC timeline."""
    pacer = ReplayPacer(speed, stop_event) if speed is not None else None
    count = 0
    for timestamp, port, line in merge_logged_streams(adsb_lines, mlat_lines):
        if stop_event.is_set():
            break
        if pacer is not None and pacer.pace(timestamp):
            break
        clients[port].sendall(line.rstrip("\r\n").encode("utf-8") + b"\r\n")
        count += 1
    return count


class ReplayPacer:
    """Schedule messages against a monotonic clock without reordering them."""

    def __init__(
        self,
        speed: float,
        stop_event: threading.Event,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        if speed <= 0:
            raise ValueError("speed must be positive")
        self.speed = speed
        self.stop_event = stop_event
        self.monotonic = monotonic
        self.wait = wait or stop_event.wait
        self._recording_start: datetime | None = None
        self._wall_start: float | None = None
        self.backward_timestamps = 0
        self._previous: datetime | None = None

    def pace(self, timestamp: datetime) -> bool:
        if self._recording_start is None:
            self._recording_start = timestamp
            self._wall_start = self.monotonic()
        if self._previous is not None and timestamp < self._previous:
            self.backward_timestamps += 1
        effective = max(timestamp, self._previous) if self._previous else timestamp
        self._previous = effective
        offset = (effective - self._recording_start).total_seconds() / self.speed
        delay = self._wall_start + offset - self.monotonic()  # type: ignore[operator]
        return delay > 0 and self.wait(delay)


def replay_lines(
    lines: Iterable[str],
    client: socket.socket,
    speed: float | None,
    stop_event: threading.Event,
) -> tuple[int, int]:
    """Send lines in file order. A speed of None means no intentional delay."""
    pacer = ReplayPacer(speed, stop_event) if speed is not None else None
    count = 0
    for line in lines:
        if stop_event.is_set():
            break
        if pacer is not None and pacer.pace(message_timestamp(line)):
            break
        payload = line.rstrip("\r\n").encode("utf-8") + b"\r\n"
        client.sendall(payload)
        count += 1
    return count, pacer.backward_timestamps if pacer else 0


class ReplayServer:
    """Serve one recorded stream and keep Transit Warning's other port quiet."""

    def __init__(
        self,
        scenario: Scenario,
        source_path: Path,
        speed: float | None,
        host: str = "127.0.0.1",
    ) -> None:
        self.scenario = scenario
        self.source_path = source_path
        self.speed = speed
        self.host = host
        self.stop_event = threading.Event()
        self._listeners: list[socket.socket] = []
        self._threads: list[threading.Thread] = []

    def _listen(self, port: int) -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, port))
        listener.listen(1)
        listener.settimeout(0.5)
        self._listeners.append(listener)
        return listener

    def start(self) -> None:
        if not self.source_path.is_file():
            raise FileNotFoundError(self.source_path)
        # Bind both before starting either worker, so startup is atomic.
        active = self._listen(self.scenario.active_port)
        try:
            silent = self._listen(self.scenario.silent_port)
        except Exception:
            active.close()
            self._listeners.clear()
            raise
        self._threads = [
            threading.Thread(target=self._active_worker, args=(active,), daemon=True),
            threading.Thread(target=self._silent_worker, args=(silent,), daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _accept(self, listener: socket.socket) -> socket.socket | None:
        while not self.stop_event.is_set():
            try:
                client, _ = listener.accept()
                client.settimeout(None)
                return client
            except socket.timeout:
                continue
            except OSError:
                return None
        return None

    def _active_worker(self, listener: socket.socket) -> None:
        while not self.stop_event.is_set():
            client = self._accept(listener)
            if client is None:
                return
            try:
                with client, self.source_path.open("r", encoding="utf-8", newline="") as source:
                    count, backwards = replay_lines(source, client, self.speed, self.stop_event)
                    print(f"Replay connection finished: {count} messages, "
                          f"{backwards} backward timestamps")
            except ValueError as error:
                print(f"Invalid replay message; connection closed: {error}")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as error:
                if not self.stop_event.is_set():
                    print(f"Replay client disconnected: {error}")

    def _silent_worker(self, listener: socket.socket) -> None:
        while not self.stop_event.is_set():
            client = self._accept(listener)
            if client is None:
                return
            with client:
                client.settimeout(0.5)
                while not self.stop_event.is_set():
                    try:
                        if not client.recv(1):
                            break
                    except socket.timeout:
                        continue
                    except (ConnectionResetError, ConnectionAbortedError, OSError):
                        break

    def serve_forever(self) -> None:
        self.start()
        speed = "max" if self.speed is None else f"x{self.speed:g}"
        print(f"Scenario {self.scenario.name}: {self.source_path}")
        print(f"Active {self.host}:{self.scenario.active_port} at {speed}; "
              f"silent {self.host}:{self.scenario.silent_port}")
        try:
            while not self.stop_event.wait(1):
                pass
        except KeyboardInterrupt:
            print("Stopping replay")
        finally:
            self.stop()

    def stop(self) -> None:
        self.stop_event.set()
        for listener in self._listeners:
            listener.close()
        for thread in self._threads:
            thread.join(timeout=2)


class DualReplayServer:
    """Serve two files through one scheduler after both clients connect."""

    def __init__(
        self,
        scenario: DualScenario,
        speed: float | None,
        host: str = "127.0.0.1",
        ports: tuple[int, int] = (ADSB_PORT, MLAT_PORT),
    ) -> None:
        self.scenario = scenario
        self.speed = speed
        self.host = host
        self.adsb_port, self.mlat_port = ports
        self.stop_event = threading.Event()
        self._listeners: dict[int, socket.socket] = {}
        self._thread: threading.Thread | None = None

    def _listen(self, port: int) -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, port))
        listener.listen(1)
        listener.settimeout(0.5)
        return listener

    def start(self) -> None:
        for path in (self.scenario.adsb_path, self.scenario.mlat_path):
            if not path.is_file():
                raise FileNotFoundError(path)
        try:
            self._listeners = {
                ADSB_PORT: self._listen(self.adsb_port),
                MLAT_PORT: self._listen(self.mlat_port),
            }
        except Exception:
            for listener in self._listeners.values():
                listener.close()
            self._listeners.clear()
            raise
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _accept(self, listener: socket.socket) -> socket.socket | None:
        while not self.stop_event.is_set():
            try:
                client, _ = listener.accept()
                client.settimeout(None)
                return client
            except socket.timeout:
                continue
            except OSError:
                return None
        return None

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            clients: dict[int, socket.socket] = {}
            try:
                for port in (ADSB_PORT, MLAT_PORT):
                    client = self._accept(self._listeners[port])
                    if client is None:
                        return
                    clients[port] = client
                with self.scenario.adsb_path.open("r", encoding="utf-8", newline="") as adsb_source, \
                     self.scenario.mlat_path.open("r", encoding="utf-8", newline="") as mlat_source:
                    count = replay_dual_streams(
                        adsb_source, mlat_source, clients, self.speed, self.stop_event
                    )
                    print(f"Dual replay connection finished: {count} messages")
            except ValueError as error:
                print(f"Invalid replay message; connections closed: {error}")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as error:
                if not self.stop_event.is_set():
                    print(f"Dual replay client disconnected; restarting both streams: {error}")
            finally:
                for client in clients.values():
                    client.close()

    def serve_forever(self) -> None:
        self.start()
        speed = "max" if self.speed is None else f"x{self.speed:g}"
        print(f"Scenario {self.scenario.name}: ADS-B {self.scenario.adsb_path}; "
              f"MLAT {self.scenario.mlat_path}")
        print(f"Waiting for both {self.host}:{self.adsb_port} and "
              f"{self.host}:{self.mlat_port}; shared scheduler at {speed}")
        try:
            while not self.stop_event.wait(1):
                pass
        except KeyboardInterrupt:
            print("Stopping replay")
        finally:
            self.stop()

    def stop(self) -> None:
        self.stop_event.set()
        for listener in self._listeners.values():
            listener.close()
        if self._thread is not None:
            self._thread.join(timeout=2)


def parse_speed(value: str) -> float | None:
    if value == "max":
        return None
    if value not in {"1", "10", "100"}:
        raise argparse.ArgumentTypeError("speed must be 1, 10, 100, or max")
    return float(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=tuple(SCENARIOS) + tuple(DUAL_SCENARIOS))
    parser.add_argument("--speed", default="1", type=parse_speed, metavar="{1,10,100,max}")
    parser.add_argument("--file", type=Path, help="override the scenario's recording path")
    parser.add_argument("--host", default="127.0.0.1")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.scenario in DUAL_SCENARIOS:
        if args.file is not None:
            raise SystemExit("--file is not supported for dual replay")
        DualReplayServer(DUAL_SCENARIOS[args.scenario], args.speed, args.host).serve_forever()
        return
    scenario = SCENARIOS[args.scenario]
    ReplayServer(scenario, args.file or scenario.default_path, args.speed, args.host).serve_forever()


if __name__ == "__main__":
    main()
