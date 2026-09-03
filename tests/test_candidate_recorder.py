"""Unit tests for Candidate Auto-Recorder Phase 1 in-memory pre-buffer."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from beast_intent import BeastFrame, BeastFrameParser, modes_crc
from candidate_recorder import (
    CandidatePreBuffer,
    CandidateStreamRecord,
    IcaoStreamBuffer,
    StreamType,
    attribute_beast_frame,
    attribute_raw_adsb,
    attribute_sbs_icao,
    encode_beast_wire,
    normalize_icao,
    validate_utc_datetime,
)
from mlat_beast_track import MLAT_BEAST_TIMESTAMP
from recording import SessionRecorder, StreamWriter, MlatBeastWriter, archive_session


UTC = timezone.utc
T0 = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)


def make_raw_df17(icao: int = 0x4BAA92, tc: int = 19, subtype: int = 1) -> str:
    """Construct a valid 112-bit Mode-S DF17 AVR raw line with valid CRC."""
    df = 17
    ca = 5
    header = (df << 27) | (ca << 24) | (icao & 0xFFFFFF)
    me = ((tc & 0x1F) << 51) | ((subtype & 0x7) << 48) | 0x01234567
    payload_96 = (header << 56) | (me & 0xFFFFFFFFFFFFFF)
    raw = (payload_96 << 24).to_bytes(14, "big")
    full_message = (int.from_bytes(raw, "big") | modes_crc(raw)).to_bytes(14, "big")
    return f"@{0x0097635C74DC:012X}{full_message.hex().upper()};"


def make_raw_df18(icao: int = 0x4BAA6D, cf: int = 2) -> str:
    """Construct a valid 112-bit Mode-S DF18 AVR raw line with specified CF."""
    df = 18
    header = (df << 27) | ((cf & 7) << 24) | (icao & 0xFFFFFF)
    me = 0x99010203040506
    payload_96 = (header << 56) | (me & 0xFFFFFFFFFFFFFF)
    raw = (payload_96 << 24).to_bytes(14, "big")
    full_message = (int.from_bytes(raw, "big") | modes_crc(raw)).to_bytes(14, "big")
    return f"@{0x0097635C74DC:012X}{full_message.hex().upper()};"


def make_raw_df19(icao: int = 0x4BAA92) -> str:
    """Construct a 112-bit Mode-S DF19 (Military Extended Squitter) frame."""
    df = 19
    af = 0
    header = (df << 27) | (af << 24) | (icao & 0xFFFFFF)
    payload_96 = (header << 56) | 0x0123456789ABCD
    raw = (payload_96 << 24).to_bytes(14, "big")
    full_message = (int.from_bytes(raw, "big") | modes_crc(raw)).to_bytes(14, "big")
    return f"@{0x0097635C74DC:012X}{full_message.hex().upper()};"


def make_beast_frame(
    icao: int = 0x4BAA6D,
    df: int = 18,
    cf: int = 2,
    timestamp: int = MLAT_BEAST_TIMESTAMP,
    signal: int = 42,
    frame_type: int = 0x33,
) -> BeastFrame:
    """Construct a valid 14-byte Mode-S BeastFrame with valid CRC."""
    raw_header = (df << 3) | (cf & 7)
    body = bytearray(11)
    body[0] = raw_header
    body[1:4] = icao.to_bytes(3, "big")
    body[4] = 0x99  # TC 19 subtype 1
    body[5:] = b"\x01\x02\x03\x04\x05\x06"
    raw = bytes(body) + b"\x00\x00\x00"
    modes = (int.from_bytes(raw, "big") | modes_crc(raw)).to_bytes(14, "big")
    return BeastFrame(frame_type, timestamp, signal, modes)


def make_ap_overlaid_raw(icao: int = 0x4BAA92, df: int = 4) -> str:
    """Construct a Mode-S frame where parity is AP (CRC XOR ICAO address)."""
    # 56-bit or 112-bit frame with AP parity
    body = bytearray(11)
    body[0] = (df << 3) | 2
    body[1:11] = b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0A"
    raw = bytes(body) + b"\x00\x00\x00"
    # In AP parity, the CRC remainder equals icao, so modes_crc(ap_message) != 0
    crc = modes_crc(raw)
    ap = crc ^ icao
    full_message = raw[:11] + ap.to_bytes(3, "big")
    return f"*{full_message.hex().upper()};"


class AttributionHelpersTests(unittest.TestCase):
    def test_normalize_icao(self):
        self.assertEqual("4BAA92", normalize_icao("4baa92"))
        self.assertEqual("0012AB", normalize_icao(" 0012ab "))
        self.assertIsNone(normalize_icao(""))
        self.assertIsNone(normalize_icao("12345"))
        self.assertIsNone(normalize_icao("1234567"))
        self.assertIsNone(normalize_icao("ZZZZZZ"))

    def test_validate_utc_datetime(self):
        valid = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
        self.assertEqual(valid, validate_utc_datetime(valid))

        # Naive datetime must raise ValueError
        naive = datetime(2026, 9, 3, 12, 0, 0)
        with self.assertRaises(ValueError):
            validate_utc_datetime(naive)

        # Non-UTC timezone must raise ValueError
        non_utc = datetime(2026, 9, 3, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        with self.assertRaises(ValueError):
            validate_utc_datetime(non_utc)

        # Non-datetime type
        with self.assertRaises(TypeError):
            validate_utc_datetime("2026-09-03T12:00:00Z")

    def test_attribute_sbs_icao(self):
        valid_line = (
            "MSG,3,1,1,4CA767,1,2026/08/17,20:44:17.502,2026/08/17,20:44:17.502,"
            "RYR123,35000,,,52.123,21.012,,,0,0,0,0"
        )
        self.assertEqual("4CA767", attribute_sbs_icao(valid_line))
        self.assertIsNone(attribute_sbs_icao("MSG,3,1,1"))
        self.assertIsNone(attribute_sbs_icao("MSG,3,1,1,INVALID,1"))
        self.assertIsNone(attribute_sbs_icao(""))

    def test_attribute_raw_adsb_valid_and_rejected(self):
        # Valid DF17 is accepted
        raw_df17 = make_raw_df17(0x4BAA92)
        icao, message = attribute_raw_adsb(raw_df17)
        self.assertEqual("4BAA92", icao)
        self.assertIsNotNone(message)

        # Valid DF18 with CF=2 (synthetic MLAT) is accepted
        raw_df18_cf2 = make_raw_df18(0x4BAA6D, cf=2)
        icao18, _ = attribute_raw_adsb(raw_df18_cf2)
        self.assertEqual("4BAA6D", icao18)

        # DF18 with CF=1 (anonymous TIS-B) MUST be rejected
        raw_df18_cf1 = make_raw_df18(0x4BAA6D, cf=1)
        self.assertEqual((None, None), attribute_raw_adsb(raw_df18_cf1))

        # DF19 (military) MUST be rejected (not supported/authoritative)
        raw_df19 = make_raw_df19(0x4BAA92)
        self.assertEqual((None, None), attribute_raw_adsb(raw_df19))

        # AP-overlaid frames (DF4) MUST be rejected (modes_crc != 0)
        raw_ap = make_ap_overlaid_raw(0x4BAA92, df=4)
        self.assertEqual((None, None), attribute_raw_adsb(raw_ap))

        # Corrupt CRC (flip last character)
        corrupted = raw_df17[:-2] + ("0" if raw_df17[-2] != "0" else "1") + ";"
        self.assertEqual((None, None), attribute_raw_adsb(corrupted))

        # Malformed raw lines
        self.assertEqual((None, None), attribute_raw_adsb("garbage"))
        self.assertEqual((None, None), attribute_raw_adsb("*1234;"))
        self.assertEqual((None, None), attribute_raw_adsb(""))

    def test_attribute_beast_frame_valid_and_rejected(self):
        # Type 0x33 DF18 CF=2 is accepted
        frame_mlat = make_beast_frame(0x4BAA6D, df=18, cf=2)
        self.assertEqual("4BAA6D", attribute_beast_frame(frame_mlat))

        # Type 0x33 DF17 is accepted
        frame_df17 = make_beast_frame(0x4BAA92, df=17, cf=0)
        self.assertEqual("4BAA92", attribute_beast_frame(frame_df17))

        # DF18 with CF=1 (anonymous) MUST be rejected
        frame_cf1 = make_beast_frame(0x4BAA6D, df=18, cf=1)
        self.assertIsNone(attribute_beast_frame(frame_cf1))

        # Type 0x32 (7-byte Mode-S / DF11) MUST be rejected
        short_modes = bytes.fromhex("5D4BAA6D000000")
        crc = modes_crc(short_modes)
        short_modes_crc = short_modes[:4] + crc.to_bytes(3, "big")
        frame_0x32 = BeastFrame(0x32, 100, 50, short_modes_crc)
        self.assertIsNone(attribute_beast_frame(frame_0x32))

        # Type 0x31 (Mode A/C) MUST be rejected
        ac_frame = BeastFrame(0x31, 100, 50, b"\x12\x34")
        self.assertIsNone(attribute_beast_frame(ac_frame))

        # Bad CRC
        bad_modes = bytearray(frame_mlat.modes)
        bad_modes[-1] ^= 1
        bad_frame = BeastFrame(frame_mlat.frame_type, frame_mlat.beast_timestamp, frame_mlat.signal, bytes(bad_modes))
        self.assertIsNone(attribute_beast_frame(bad_frame))

    def test_encode_beast_wire_roundtrip(self):
        original = make_beast_frame(0x1A0123)  # Contains escape byte 0x1A
        wire_bytes = encode_beast_wire(original)
        parser = BeastFrameParser()
        decoded = parser.feed(wire_bytes)
        self.assertEqual(1, len(decoded))
        self.assertEqual(original.frame_type, decoded[0].frame_type)
        self.assertEqual(original.beast_timestamp, decoded[0].beast_timestamp)
        self.assertEqual(original.signal, decoded[0].signal)
        self.assertEqual(original.modes, decoded[0].modes)


class CandidatePreBufferCoreTests(unittest.TestCase):
    def setUp(self):
        self.sim_time = T0
        self.buffer = CandidatePreBuffer(
            buffer_duration_seconds=60.0,
            clock=lambda: self.sim_time,
        )

    def test_per_icao_isolation(self):
        icao1 = "4BAA92"
        icao2 = "4CA767"

        line1 = f"MSG,3,1,1,{icao1},1,2026/09/03,12:00:00.000,2026/09/03,12:00:00.000,FL1"
        line2 = f"MSG,3,1,1,{icao2},1,2026/09/03,12:00:01.000,2026/09/03,12:00:01.000,FL2"

        self.assertTrue(self.buffer.feed_adsb_sbs(line1, T0))
        self.assertTrue(self.buffer.feed_adsb_sbs(line2, T0 + timedelta(seconds=1)))

        self.assertEqual(1, len(self.buffer.get_records(icao1)))
        self.assertEqual(1, len(self.buffer.get_records(icao2)))
        self.assertEqual(line1, self.buffer.get_records(icao1)[0].raw_data)
        self.assertEqual(line2, self.buffer.get_records(icao2)[0].raw_data)

        self.assertEqual([icao1, icao2], self.buffer.tracked_icaos())
        self.assertEqual(2, self.buffer.record_count())

        # Clearing icao1 leaves icao2 intact
        self.buffer.clear(icao1)
        self.assertFalse(self.buffer.has_icao(icao1))
        self.assertTrue(self.buffer.has_icao(icao2))
        self.assertEqual(1, len(self.buffer.get_records(icao2)))

    def test_multiple_stream_types_for_one_icao(self):
        icao = "4BAA92"
        icao_int = int(icao, 16)

        sbs_adsb = f"MSG,3,1,1,{icao},1,2026/09/03,12:00:00.000,2026/09/03,12:00:00.000,FL1"
        sbs_mlat = f"MSG,3,1,1,{icao},1,2026/09/03,12:00:01.000,2026/09/03,12:00:01.000,FL1"
        raw_adsb = make_raw_df17(icao_int)
        beast_frame = make_beast_frame(icao_int, df=18, cf=2)

        self.assertTrue(self.buffer.feed_adsb_sbs(sbs_adsb, T0))
        self.assertTrue(self.buffer.feed_mlat_sbs(sbs_mlat, T0 + timedelta(seconds=1)))
        self.assertTrue(self.buffer.feed_raw_adsb(raw_adsb, T0 + timedelta(seconds=2)))
        self.assertTrue(self.buffer.feed_mlat_beast_frame(beast_frame, T0 + timedelta(seconds=3)))

        records = self.buffer.get_records(icao)
        self.assertEqual(4, len(records))
        self.assertEqual(
            [StreamType.ADSB_SBS, StreamType.MLAT_SBS, StreamType.RAW_ADSB, StreamType.MLAT_BEAST],
            [r.stream_type for r in records],
        )

        # Check raw representations preserved faithfully
        self.assertEqual(sbs_adsb, records[0].raw_data)
        self.assertEqual(sbs_mlat, records[1].raw_data)
        self.assertEqual(raw_adsb, records[2].raw_data)
        self.assertIsInstance(records[3].raw_data, bytes)
        self.assertEqual(encode_beast_wire(beast_frame), records[3].raw_data)

    def test_60_second_pruning(self):
        icao = "4BAA92"
        line = f"MSG,3,1,1,{icao},1,2026/09/03,12:00:00.000,2026/09/03,12:00:00.000,FL1"

        # Record at T0
        self.buffer.feed_adsb_sbs(line, T0)
        # Record at T0 + 10s
        self.buffer.feed_adsb_sbs(line, T0 + timedelta(seconds=10))
        # Record at T0 + 30s
        self.buffer.feed_adsb_sbs(line, T0 + timedelta(seconds=30))

        # At T0 + 65s, cutoff is T0 + 5s. The T0 record (age 65s) must be pruned.
        pruned = self.buffer.prune(T0 + timedelta(seconds=65))
        self.assertEqual(1, pruned)

        # Append record at T0 + 65s
        self.sim_time = T0 + timedelta(seconds=65)
        self.buffer.feed_adsb_sbs(line, T0 + timedelta(seconds=65))

        records = self.buffer.get_records(icao)
        self.assertEqual(3, len(records))
        self.assertEqual(T0 + timedelta(seconds=10), records[0].received_at_utc)
        self.assertEqual(T0 + timedelta(seconds=30), records[1].received_at_utc)
        self.assertEqual(T0 + timedelta(seconds=65), records[2].received_at_utc)

    def test_out_of_order_timestamps(self):
        icao = "4BAA92"
        line = f"MSG,3,1,1,{icao},1,2026/09/03,12:00:00.000,2026/09/03,12:00:00.000,FL1"

        # Ingest records deliberately out of chronological order: T+10s, then T+5s, then T+20s, then T+8s
        self.buffer.feed_adsb_sbs(line, T0 + timedelta(seconds=10))
        self.buffer.feed_adsb_sbs(line, T0 + timedelta(seconds=5))
        self.buffer.feed_adsb_sbs(line, T0 + timedelta(seconds=20))
        self.buffer.feed_adsb_sbs(line, T0 + timedelta(seconds=8))

        # get_records MUST return records sorted strictly chronologically
        ordered = self.buffer.get_records(icao)
        self.assertEqual(4, len(ordered))
        self.assertEqual(
            [T0 + timedelta(seconds=s) for s in (5, 8, 10, 20)],
            [r.received_at_utc for r in ordered],
        )

        # Prune with cutoff at T0 + 9s (i.e. now = T0 + 69s with 60s window)
        # T+5s and T+8s must be pruned even though T+10s was inserted first
        pruned = self.buffer.prune(T0 + timedelta(seconds=69))
        self.assertEqual(2, pruned)

        surviving = self.buffer.get_records(icao)
        self.assertEqual(2, len(surviving))
        self.assertEqual(T0 + timedelta(seconds=10), surviving[0].received_at_utc)
        self.assertEqual(T0 + timedelta(seconds=20), surviving[1].received_at_utc)

    def test_future_timestamp_poisoning_prevention(self):
        icao = "4BAA92"
        legitimate_line = f"MSG,3,1,1,{icao},1,2026/09/03,12:00:00.000,2026/09/03,12:00:00.000,FL1"

        # Ingest legitimate record at T0
        self.assertTrue(self.buffer.feed_adsb_sbs(legitimate_line, T0))
        self.assertEqual(1, self.buffer.record_count())

        # Attempt to feed a record with an erroneous future timestamp (1 day in the future)
        rogue_future_time = T0 + timedelta(days=1)
        rogue_line = f"MSG,3,1,1,999999,1,2026/09/04,12:00:00.000,2026/09/04,12:00:00.000,FL1"
        self.assertFalse(self.buffer.feed_adsb_sbs(rogue_line, rogue_future_time))

        # Buffer state for legitimate ICAO must remain intact and not wiped out
        self.assertTrue(self.buffer.has_icao(icao))
        self.assertEqual(1, self.buffer.record_count())
        self.assertFalse(self.buffer.has_icao("999999"))

        # Subsequent normal records continue to be accepted and auto-pruned normally
        self.sim_time = T0 + timedelta(seconds=5)
        self.assertTrue(self.buffer.feed_adsb_sbs(legitimate_line, T0 + timedelta(seconds=5)))
        self.assertEqual(2, len(self.buffer.get_records(icao)))

    def test_naive_datetime_rejection(self):
        line = "MSG,3,1,1,4BAA92,1,2026/09/03,12:00:00.000,2026/09/03,12:00:00.000,FL1"
        naive_dt = datetime(2026, 9, 3, 12, 0, 0)  # No tzinfo

        with self.assertRaises(ValueError):
            self.buffer.feed_adsb_sbs(line, naive_dt)

        with self.assertRaises(ValueError):
            self.buffer.feed_mlat_sbs(line, naive_dt)

        with self.assertRaises(ValueError):
            self.buffer.feed_raw_adsb(make_raw_df17(0x4BAA92), naive_dt)

        with self.assertRaises(ValueError):
            self.buffer.feed_mlat_beast_frame(make_beast_frame(0x4BAA92), naive_dt)

        with self.assertRaises(ValueError):
            self.buffer.feed_mlat_beast_chunk(b"\x1a\x33...", naive_dt)

        with self.assertRaises(ValueError):
            self.buffer.get_records_since("4BAA92", naive_dt)

        with self.assertRaises(ValueError):
            self.buffer.prune(naive_dt)

    def test_stale_icao_cleanup(self):
        icao = "4BAA92"
        line = f"MSG,3,1,1,{icao},1,2026/09/03,12:00:00.000,2026/09/03,12:00:00.000,FL1"

        self.buffer.feed_adsb_sbs(line, T0)
        self.assertTrue(self.buffer.has_icao(icao))
        self.assertIn(icao, self.buffer.tracked_icaos())

        # Advance time by 70s (> 60s window) and prune
        pruned = self.buffer.prune(T0 + timedelta(seconds=70))
        self.assertEqual(1, pruned)

        # Buffer is empty, so ICAO entry must be completely removed from active memory
        self.assertFalse(self.buffer.has_icao(icao))
        self.assertNotIn(icao, self.buffer.tracked_icaos())
        self.assertEqual(0, self.buffer.record_count())
        self.assertEqual([], self.buffer.get_records(icao))

        # Subsequent messages for that ICAO cleanly re-create the buffer
        self.sim_time = T0 + timedelta(seconds=75)
        self.buffer.feed_adsb_sbs(line, T0 + timedelta(seconds=75))
        self.assertTrue(self.buffer.has_icao(icao))
        self.assertEqual(1, len(self.buffer.get_records(icao)))

    def test_stale_before_active_capacity_eviction(self):
        # Buffer limited to 2 concurrent ICAOs
        cap_buffer = CandidatePreBuffer(
            buffer_duration_seconds=60.0,
            max_icaos=2,
            clock=lambda: self.sim_time,
        )
        line_a = "MSG,3,1,1,AAAAAA,1,2026/09/03,12:00:00.000,2026/09/03,12:00:00.000,FL1"
        line_b = "MSG,3,1,1,BBBBBB,1,2026/09/03,12:00:00.000,2026/09/03,12:00:00.000,FL2"
        line_c = "MSG,3,1,1,CCCCCC,1,2026/09/03,12:00:00.000,2026/09/03,12:00:00.000,FL3"

        # Feed A and B at T0
        self.sim_time = T0
        cap_buffer.feed_adsb_sbs(line_a, T0)
        cap_buffer.feed_adsb_sbs(line_b, T0)
        self.assertEqual(["AAAAAA", "BBBBBB"], cap_buffer.tracked_icaos())

        # Advance time by 65s. Keep B active with a fresh record at T0+65s. A is now stale (>60s old).
        self.sim_time = T0 + timedelta(seconds=65)
        cap_buffer.feed_adsb_sbs(line_b, self.sim_time)

        # Now feed C. Since capacity (2) is reached, it must prune stale buffer A first rather than evicting active B!
        cap_buffer.feed_adsb_sbs(line_c, self.sim_time)

        # Stale A was pruned, while active B and new C are retained!
        self.assertNotIn("AAAAAA", cap_buffer.tracked_icaos())
        self.assertIn("BBBBBB", cap_buffer.tracked_icaos())
        self.assertIn("CCCCCC", cap_buffer.tracked_icaos())
        self.assertEqual(2, len(cap_buffer.tracked_icaos()))

    def test_metadata_immutability_safety(self):
        raw_line = make_raw_df17(0x4BAA92)
        self.buffer.feed_raw_adsb(raw_line, T0)

        records = self.buffer.get_records("4BAA92")
        self.assertEqual(1, len(records))
        metadata = records[0].metadata
        self.assertIsNotNone(metadata)
        self.assertIn("modes_hex", metadata)

        # Attempting to mutate metadata mapping must raise TypeError
        with self.assertRaises(TypeError):
            metadata["modes_hex"] = "MUTATED"

        with self.assertRaises(TypeError):
            metadata["new_key"] = "MUTATED"

    def test_malformed_unattributable_raw_rejection(self):
        # Empty line
        self.assertFalse(self.buffer.feed_raw_adsb("", T0))
        # Invalid string
        self.assertFalse(self.buffer.feed_raw_adsb("not_a_mode_s_frame", T0))
        # Corrupt CRC
        raw_valid = make_raw_df17(0x4BAA92)
        corrupted = raw_valid[:-2] + ("0" if raw_valid[-2] != "0" else "1") + ";"
        self.assertFalse(self.buffer.feed_raw_adsb(corrupted, T0))
        # Incomplete hex length
        self.assertFalse(self.buffer.feed_raw_adsb("*8D4BAA92;", T0))

        self.assertEqual(0, self.buffer.record_count())
        self.assertEqual([], self.buffer.tracked_icaos())

    def test_beast_framing_across_arbitrary_tcp_chunk_boundaries(self):
        icao1 = 0x4BAA6D
        icao2 = 0x4BAA92
        frame1 = make_beast_frame(icao1, df=18, cf=2)
        frame2 = make_beast_frame(icao2, df=17, cf=0)

        wire1 = encode_beast_wire(frame1)
        wire2 = encode_beast_wire(frame2)
        full_stream = wire1 + wire2

        # Feed the combined stream in 3-byte chunks to stress boundary assembly
        chunk_size = 3
        all_attributed = []
        for i in range(0, len(full_stream), chunk_size):
            chunk = full_stream[i : i + chunk_size]
            attributed = self.buffer.feed_mlat_beast_chunk(chunk, T0)
            all_attributed.extend(attributed)

        self.assertEqual(["4BAA6D", "4BAA92"], all_attributed)
        self.assertEqual(["4BAA6D", "4BAA92"], self.buffer.tracked_icaos())

        rec1 = self.buffer.get_records("4BAA6D")
        rec2 = self.buffer.get_records("4BAA92")
        self.assertEqual(1, len(rec1))
        self.assertEqual(1, len(rec2))
        self.assertEqual(wire1, rec1[0].raw_data)
        self.assertEqual(wire2, rec2[0].raw_data)

    def test_unattributable_beast_rejection(self):
        # Mode A/C frame (type 0x31)
        ac_wire = b"\x1a\x31\x00\x00\x00\x00\x00\x01\x10\x12\x34"
        self.assertEqual([], self.buffer.feed_mlat_beast_chunk(ac_wire, T0))

        # Corrupt CRC
        frame = make_beast_frame(0x4BAA6D)
        bad_modes = bytearray(frame.modes)
        bad_modes[-1] ^= 0xFF
        bad_frame = BeastFrame(frame.frame_type, frame.beast_timestamp, frame.signal, bytes(bad_modes))
        self.assertFalse(self.buffer.feed_mlat_beast_frame(bad_frame, T0))

        # Random garbage bytes
        self.assertEqual([], self.buffer.feed_mlat_beast_chunk(b"random garbage noise", T0))

        self.assertEqual(0, self.buffer.record_count())

    def test_no_candidate_disk_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            files_before = list(temp_path.rglob("*"))

            # Run operations in pre-buffer
            buffer = CandidatePreBuffer(buffer_duration_seconds=60.0)
            buffer.feed_adsb_sbs("MSG,3,1,1,4CA767,1,2026/09/03,12:00:00.000,2026/09/03,12:00:00.000,FL1", T0)
            buffer.feed_raw_adsb(make_raw_df17(0x4BAA92), T0)
            buffer.feed_mlat_beast_chunk(encode_beast_wire(make_beast_frame(0x4BAA6D)), T0)
            buffer.prune(T0 + timedelta(seconds=70))

            files_after = list(temp_path.rglob("*"))
            self.assertEqual(files_before, files_after)

    def test_bounded_memory_limits(self):
        # Test max_records_per_icao
        small_buffer = CandidatePreBuffer(
            max_records_per_icao=3,
            max_icaos=2,
            clock=lambda: self.sim_time,
        )
        icao = "4BAA92"
        for i in range(5):
            line = f"MSG,3,1,1,{icao},1,2026/09/03,12:00:0{i}.000,2026/09/03,12:00:0{i}.000,FL{i}"
            small_buffer.feed_adsb_sbs(line, T0 + timedelta(seconds=i))

        records = small_buffer.get_records(icao)
        self.assertEqual(3, len(records))
        self.assertEqual(T0 + timedelta(seconds=2), records[0].received_at_utc)
        self.assertEqual(T0 + timedelta(seconds=4), records[2].received_at_utc)

        # Test max_icaos eviction when all are active
        small_buffer.feed_adsb_sbs("MSG,3,1,1,AAAAAA,1,2026/09/03,12:00:10.000,2026/09/03,12:00:10.000,FL1", T0 + timedelta(seconds=10))
        small_buffer.feed_adsb_sbs("MSG,3,1,1,BBBBBB,1,2026/09/03,12:00:11.000,2026/09/03,12:00:11.000,FL2", T0 + timedelta(seconds=11))

        # Oldest ICAO (4BAA92) should have been evicted to keep capacity <= 2
        self.assertEqual(2, len(small_buffer.tracked_icaos()))
        self.assertNotIn(icao, small_buffer.tracked_icaos())
        self.assertIn("AAAAAA", small_buffer.tracked_icaos())
        self.assertIn("BBBBBB", small_buffer.tracked_icaos())

    def test_get_records_since(self):
        icao = "4BAA92"
        line = f"MSG,3,1,1,{icao},1,2026/09/03,12:00:00.000,2026/09/03,12:00:00.000,FL1"
        self.buffer.feed_adsb_sbs(line, T0)
        self.buffer.feed_adsb_sbs(line, T0 + timedelta(seconds=10))
        self.buffer.feed_adsb_sbs(line, T0 + timedelta(seconds=20))

        since_15 = self.buffer.get_records_since(icao, T0 + timedelta(seconds=15))
        self.assertEqual(1, len(since_15))
        self.assertEqual(T0 + timedelta(seconds=20), since_15[0].received_at_utc)

        since_0 = self.buffer.get_records_since(icao, T0)
        self.assertEqual(3, len(since_0))

        # Unknown ICAO returns empty
        self.assertEqual([], self.buffer.get_records_since("000000", T0))

    def test_concurrent_multithreaded_ingestion(self):
        import threading
        buffer = CandidatePreBuffer(
            buffer_duration_seconds=60.0,
            clock=lambda: self.sim_time,
        )
        errors = []

        def worker(icao_id: int):
            try:
                icao_str = f"{icao_id:06X}"
                for s in range(20):
                    line = f"MSG,3,1,1,{icao_str},1,2026/09/03,12:00:{s:02d}.000,2026/09/03,12:00:{s:02d}.000,FL1"
                    buffer.feed_adsb_sbs(line, T0 + timedelta(seconds=s))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual([], errors)
        self.assertEqual(10, len(buffer.tracked_icaos()))
        self.assertEqual(200, buffer.record_count())

    def test_existing_full_recorder_coexistence_unaffected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir) / "sessions"
            pre_buffer = CandidatePreBuffer(buffer_duration_seconds=60.0)

            # Initialize full SessionRecorder with all 4 streams enabled
            recorder = SessionRecorder(
                T0,
                adsb_port=30003,
                mlat_port=30106,
                adsb_timestamp_timezone="Europe/Warsaw",
                base_dir=base_dir,
                raw_port=30002,
                mlat_beast_port=30105,
            )

            # Concurrent streaming data: feed both full recorder and candidate pre-buffer
            sbs_line = "MSG,3,1,1,4BAA92,1,2026/09/03,12:00:00.000,2026/09/03,12:00:00.000,FL1\n"
            raw_line = make_raw_df17(0x4BAA92) + "\n"
            beast_fr = make_beast_frame(0x4BAA6D)
            beast_wire = encode_beast_wire(beast_fr)

            # Feed SessionRecorder
            self.assertTrue(recorder.record_line(30003, sbs_line))
            self.assertTrue(recorder.record_line(30106, sbs_line))
            self.assertTrue(recorder.record_line(30002, raw_line))
            self.assertTrue(recorder.record_raw_diagnostic_event({"diagnostic": "test", "icao": "4BAA92"}))
            self.assertTrue(recorder.record_mlat_beast_bytes(beast_wire))
            self.assertTrue(recorder.record_mlat_beast_event(beast_fr, T0, tc19_update=True))

            # Feed CandidatePreBuffer concurrently
            self.assertTrue(pre_buffer.feed_adsb_sbs(sbs_line, T0))
            self.assertTrue(pre_buffer.feed_raw_adsb(raw_line, T0))
            attributed = pre_buffer.feed_mlat_beast_chunk(beast_wire, T0)
            self.assertEqual(["4BAA6D"], attributed)

            # Close and archive full session
            recorder.close(T0 + timedelta(seconds=10))
            self.assertTrue(recorder.manifest_path.is_file())

            # Full archive verification
            archived = archive_session(recorder.session_dir)
            self.assertTrue(archived)
            self.assertTrue((recorder.session_dir / "streams.zip").is_file())

            # Pre-buffer retained only candidate records without writing any files
            self.assertEqual(2, len(pre_buffer.get_records("4BAA92")))
            self.assertEqual(1, len(pre_buffer.get_records("4BAA6D")))
            self.assertEqual(3, pre_buffer.record_count())
            self.assertFalse((base_dir / "candidates").exists())


if __name__ == "__main__":
    unittest.main()
