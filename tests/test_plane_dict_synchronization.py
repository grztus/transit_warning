import datetime
import threading
import unittest
from unittest.mock import patch

import transit_warning_v5 as transit


def plane_entry(timestamp, distance="999"):
    return [
        timestamp, "", "", "", "", distance, "", "", "", "", "", "", "", "", "",
        [], [], "", "", "", "", "", "", "", "", "", "", "", "", "", None, False,
    ]


def msg1(icao):
    timestamp = transit.clock.now_utc()
    return (
        f"MSG,1,1,1,{icao},1,{timestamp:%Y/%m/%d},{timestamp:%H:%M:%S.%f},"
        f"{timestamp:%Y/%m/%d},{timestamp:%H:%M:%S.%f},TEST123"
    )


class IterationControlledDict(dict):
    def __init__(self, *args, iteration_started, mutation_attempted, **kwargs):
        super().__init__(*args, **kwargs)
        self.iteration_started = iteration_started
        self.mutation_attempted = mutation_attempted

    def __iter__(self):
        self.iteration_started.set()
        if not self.mutation_attempted.wait(2):
            raise AssertionError("mutation thread did not reach the controlled interleaving")
        return super().__iter__()


class ItemsControlledDict(dict):
    def __init__(self, *args, iteration_started, mutation_attempted, **kwargs):
        super().__init__(*args, **kwargs)
        self.iteration_started = iteration_started
        self.mutation_attempted = mutation_attempted

    def items(self):
        self.iteration_started.set()
        if not self.mutation_attempted.wait(2):
            raise AssertionError("processing thread did not reach the controlled interleaving")
        return super().items()


class PlaneDictSynchronizationTests(unittest.TestCase):
    def setUp(self):
        self.original_plane_dict = transit.plane_dict
        self.original_last_t = transit.last_t

    def tearDown(self):
        transit.plane_dict = self.original_plane_dict
        transit.last_t = self.original_last_t

    def run_threads(self, *targets):
        errors = []

        def guarded(target):
            try:
                target()
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=guarded, args=(target,)) for target in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), "synchronized operation did not finish")
        self.assertEqual(errors, [])

    def test_tabela_and_new_icao_use_the_same_lock(self):
        iteration_started = threading.Event()
        mutation_attempted = threading.Event()
        now = transit.clock.now_utc()
        transit.plane_dict = IterationControlledDict(
            {"AAA001": plane_entry(now), "AAA002": plane_entry(now)},
            iteration_started=iteration_started,
            mutation_attempted=mutation_attempted,
        )
        transit.last_t = now - datetime.timedelta(seconds=2)

        def add_new_icao():
            self.assertTrue(iteration_started.wait(2))
            mutation_attempted.set()
            transit.process_line(msg1("NEW001"), 30106)

        with patch.object(transit, "clear_screen", return_value=None), patch("builtins.print"):
            self.run_threads(transit.tabela, add_new_icao)

        self.assertIn("NEW001", transit.plane_dict)

    def test_cleaning_and_message_processing_are_serialized(self):
        iteration_started = threading.Event()
        processing_attempted = threading.Event()
        old = transit.clock.now_utc() - datetime.timedelta(seconds=transit.MAX_AGE_SECONDS + 1)
        transit.plane_dict = ItemsControlledDict(
            {"OLD001": plane_entry(old)},
            iteration_started=iteration_started,
            mutation_attempted=processing_attempted,
        )

        def process_new_icao():
            self.assertTrue(iteration_started.wait(2))
            processing_attempted.set()
            transit.process_line(msg1("NEW002"), 30106)

        with patch.object(transit, "tabela", return_value=(0, 0, 0, 0)):
            self.run_threads(transit.clean_dict, process_new_icao)

        self.assertNotIn("OLD001", transit.plane_dict)
        self.assertIn("NEW002", transit.plane_dict)


if __name__ == "__main__":
    unittest.main()
