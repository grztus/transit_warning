"""Private, asynchronous encounter termination journal; never a public DTO."""

from copy import deepcopy
import json
from pathlib import Path
import queue
import threading


class FinalizationJournal:
    def __init__(self, directory, capacity=4096):
        self.directory = Path(directory)
        self.queue = queue.Queue(maxsize=capacity)
        self.dropped = 0
        self.failed = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def append(self, record):
        try:
            self.queue.put_nowait(deepcopy(record))
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _run(self):
        while True:
            record = self.queue.get()
            try:
                if record is None:
                    return
                self.directory.mkdir(parents=True, exist_ok=True)
                path = self.directory / (record['finalized_at_utc'][:10] + '.jsonl')
                with path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(record, allow_nan=False) + '\n')
            except Exception:
                self.failed += 1
            finally:
                self.queue.task_done()

    def close(self):
        self.queue.put(None)
        self.thread.join()
