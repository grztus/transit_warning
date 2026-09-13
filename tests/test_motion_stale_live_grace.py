"""MLAT cadence regressions through real ingest and dashboard lifecycle."""
import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from app_backend.contracts import serialize_live_state

import transit_warning as r
from authoritative_transit import AuthoritativeTransitLifecycle
from live_dashboard import DashboardRuntime, DashboardState
from observer_position import ObserverContext, ObserverPosition
from shadow_2d_prediction import Shadow2DConfig
from transit_clock import ReplayClock
from test_motion_freshness import NOW, sbs


ICAO = '4BA905'


class MotionStaleLiveGraceTests(unittest.TestCase):
    def setUp(self):
        for name in ('plane_dict', 'altitude_sources', 'aircraft_motion_states',
                     'raw_adsb_tracks', 'raw_adsb_versions', 'gnss_altitude_states',
                     'mlat_beast_tracks', 'mlat_coarse_tracks', 'aircraft_intent_states',
                     'aircraft_motion_freshness_status', 'sun_prediction_last_valid',
                     'moon_prediction_last_valid', 'sun_predicted_transit_utc',
                     'moon_predicted_transit_utc', 'transit_solver_diagnostics',
                     'vertical_transit_diagnostics', 'geometric_altitude_selections',
                     'authoritative_terminal_predictions'):
            self.enterContext(patch.object(r, name, {}))
        values = dict(clock=ReplayClock(), replay_time_initialized=False,
            aircraft_source_mode='LOCAL', adsb_port=30003, mlat_port=30106,
            adsb_timestamp_timezone='UTC', pressure=1013.25, metric_units=True,
            authoritative_transit_lifecycle=AuthoritativeTransitLifecycle('LEGACY'),
            shadow_2d_config=Shadow2DConfig(enabled=False), deferred_true2d=None,
            transit_snapshot_manager=None, candidate_encounter_manager=None,
            candidate_pre_buffer=None, candidate_storage_worker=None,
            mlat_beast_enabled=False, aircraft_los_geoid_provider=None,
            aircraft_los_geometry_mode='LEGACY', sun_alt=30., moon_alt=20.,
            sun_az=120., moon_az=90., dashboard_sep_visible_max_deg=2.)
        for name, value in values.items():
            self.enterContext(patch.object(r, name, value, create=True))
        r.clock.advance_to(NOW)
        self.observer = ObserverContext(ObserverPosition(51., 21., 200.), 'STATIC', 'STATIC', epoch=7)
        self.enterContext(patch.object(r, 'current_observer_context', return_value=self.observer))
        self.enterContext(patch.object(r, 'tabela_for_observer', return_value=(30., 120., 20., 90.)))
        self.enterContext(patch.object(r, 'tabela', return_value=(30., 120., 20., 90.)))
        for name in ('gong', 'apply_replay_environment', 'apply_replay_raw_diagnostics',
                     'capture_transit_observation', 'capture_transit_prediction',
                     'cancel_pending_transit_notification'):
            self.enterContext(patch.object(r, name))
        self.enterContext(patch.object(r, 'get_metar_press', return_value=1013.25))
        self.enterContext(patch.object(r, 'emit_transit_notification', return_value=False))
        self.enterContext(patch.object(r, 'apply_vertical_prediction_to_transit_result',
                                      side_effect=lambda icao, body, result, *args: result))
        self.solve = self.enterContext(patch.object(r, 'moving_body_transit_pred', side_effect=self.prediction))
        self.events = []
        self.dashboard = DashboardRuntime(DashboardState(
            finalization_journal=SimpleNamespace(append=self.events.append)))
        self.enterContext(patch.object(r, 'dashboard_runtime', self.dashboard))
        self.target = NOW + datetime.timedelta(seconds=300)

    def prediction(self, *args, **kwargs):
        remaining = (self.target-r.clock.now_utc()).total_seconds()
        return (51.2, 21.2, 120., 25., 17.9, 33.7, remaining, 0, 120., 24.81, None)

    def send(self, sec, subtype=3, prefix='MLAT', *, track='', speed='', position=True,
             logged=None):
        when = NOW + datetime.timedelta(seconds=sec)
        line = sbs(prefix, subtype, when.strftime('%Y/%m/%d %H:%M:%S.%f'), ICAO,
            altitude=10000 if position else '', groundspeed=speed, track=track,
            latitude=51.2+sec*.0001 if position else '', longitude=21.2 if position else '')
        if logged is not None:
            parts = line.split(',')
            parts[8], parts[9] = (NOW+datetime.timedelta(seconds=logged)).strftime('%Y/%m/%d %H:%M:%S.%f').split()
            line = ','.join(parts)
        r.process_line(line, r.mlat_port)
        if ICAO in r.plane_dict:
            r.plane_dict[ICAO][1] = 'TKJ9BU'

    def establish(self, velocity_field='both'):
        self.send(0, track=180, speed=450)
        for t in (1, 2, 3.2, 4, 5, 6.4, 7, 8, 9, 9.8, 10):
            self.send(t, track=180 if velocity_field == 'speed' else '',
                      speed=450 if velocity_field == 'track' else '')
        self.assertTrue(self.live())

    def live(self, body='sun'):
        return self.dashboard.state.snapshot(r.clock.now_utc())[body]['candidates']

    def test_short_mlat_velocity_gap_freezes_live_without_solving_then_recovers(self):
        self.establish()
        previous = self.live()[0]
        self.solve.reset_mock()
        self.send(11)
        freshness = r.get_aircraft_motion_freshness_status(ICAO)
        self.assertEqual(r.MotionFreshnessStatus.STALE, freshness.status)
        self.assertEqual(0., freshness.position_age)
        self.assertIn('TRACK_AGE_GT_10', freshness.reason_codes)
        self.assertIn(ICAO, r.plane_dict)
        self.assertEqual(289., r.predicted_transit_remaining_seconds(ICAO, 'sun'))
        self.solve.assert_not_called()
        self.assertTrue(self.live(), 'Short MLAT velocity gap must retain the prior LIVE candidate')
        self.assertEqual(previous['last_prediction_update_utc'], self.live()[0]['last_prediction_update_utc'])
        self.assertEqual('DEGRADED', self.live()[0]['prediction_quality'])
        self.assertEqual([], self.events)
        self.send(11.8, subtype=4, prefix='MSG', position=False, track=180, speed=450)
        self.assertTrue(self.live())
        self.assertEqual(2, self.solve.call_count)
        self.assertEqual('FRESH', self.live()[0]['prediction_quality'])
        self.assertIsNone(self.live()[0]['prediction_expires_utc'])
        self.assertIsNone(self.live()[0]['prediction_quality_reason'])

    def test_public_contract_is_compact_and_stale_input_never_refreshes_geometry(self):
        self.establish()
        original = self.live()[0]
        self.solve.reset_mock()
        for t in (11, 12, 13, 14, 16, 18, 19.99):
            self.send(t)
            current = serialize_live_state(self.dashboard.state.snapshot(r.clock.now_utc()))['bodies']['sun']['candidates'][0]
            self.assertEqual('DEGRADED', current['prediction_quality'])
            self.assertEqual('VELOCITY_STALE', current['prediction_quality_reason'])
            self.assertEqual('2026-08-19T12:00:20Z', current['prediction_expires_utc'])
            for field in ('last_prediction_update_utc', 'predicted_event_utc',
                          'separation_deg', 'body_azimuth_deg', 'body_elevation_deg',
                          'aircraft_elevation_deg', 'distance_km'):
                self.assertEqual(original[field], current[field])
            self.assertNotIn('motion_freshness', current)
        self.solve.assert_not_called()

    def test_recovered_candidate_reaches_passed_without_duplicate_finalization(self):
        self.target = NOW+datetime.timedelta(seconds=21)
        self.establish()
        self.send(11)
        self.assertEqual('DEGRADED', self.live()[0]['prediction_quality'])
        self.send(16, track=180, speed=450)
        self.assertEqual('FRESH', self.live()[0]['prediction_quality'])
        self.send(20, track=180, speed=450)
        self.dashboard.tick(self.target)
        self.dashboard.tick(self.target+datetime.timedelta(seconds=1))
        self.assertEqual(['PASSED', 'PASSED'], [e['final_state'] for e in self.events])
        self.assertEqual(2, len(self.dashboard.state.query_history()['records']))

    def test_dashboard_tick_expires_degraded_candidate_at_fixed_deadline(self):
        self.establish()
        self.send(11)
        self.dashboard.tick(NOW+datetime.timedelta(seconds=20))
        self.assertFalse(self.live())
        self.assertEqual(2, len(self.events))
        self.dashboard.tick(NOW+datetime.timedelta(seconds=21))
        self.assertEqual(2, len(self.events))
        self.assertTrue(all(e['reason'] == 'MOTION_STALE' for e in self.events))

    def test_only_first_degraded_transition_marks_publication_dirty(self):
        self.establish()
        with patch.object(self.dashboard, '_publish_application_state') as publish:
            for t in (11, 12, 13, 14):
                self.send(t)
            publish.assert_called_once()

    def test_observer_invalidation_closes_degraded_display(self):
        self.establish()
        self.send(11)
        r.invalidate_observer_dependent_state(self.observer)
        self.assertFalse(self.live())

    def test_independent_track_and_groundspeed_gaps(self):
        for field in ('track', 'speed'):
            with self.subTest(field=field):
                # Reset the replay and previous encounter between independent streams.
                r.clock = ReplayClock()
                r.clock.advance_to(NOW)
                r.plane_dict.clear()
                r.aircraft_motion_states.clear()
                self.establish(field)
                self.solve.reset_mock()
                self.send(11, track=180 if field == 'speed' else '',
                          speed=450 if field == 'track' else '')
                self.assertTrue(self.live())
                self.solve.assert_not_called()
                self.send(20, track=180 if field == 'speed' else '',
                          speed=450 if field == 'track' else '')
                self.assertFalse(self.live())

    def test_no_hold_restart_from_bursts_or_short_message_gaps(self):
        self.establish()
        self.solve.reset_mock()
        for t in (11.2, 11.21, 11.22, 12.7, 13, 16, 19.99):
            self.send(t)
            self.assertTrue(self.live())
            self.assertEqual('2026-08-19T12:00:20Z', self.live()[0]['prediction_expires_utc'])
        self.send(20)
        self.assertFalse(self.live())
        self.solve.assert_not_called()
        self.assertNotIn(ICAO, r.sun_predicted_transit_utc)
        self.assertEqual(['MOTION_STALE'] * 2, [e['reason'] for e in self.events])

    def test_last_success_grace_not_first_stale_arrival(self):
        self.send(0, track=180, speed=450)
        self.send(11)
        self.assertFalse(self.live())
        self.assertIn(ICAO, r.plane_dict)

    def test_hold_expires_on_maintenance_without_more_position_messages(self):
        self.establish()
        self.send(11)
        self.assertTrue(self.live())
        r.clock.advance_to(NOW+datetime.timedelta(seconds=14.1))
        r.clean_dict()
        self.assertFalse(self.live())
        self.assertEqual(2, len(self.events))
        r.clean_dict()
        self.assertEqual(2, len(self.events))

    def test_missing_fields_and_stale_position_are_not_velocity_grace(self):
        self.send(0)
        self.assertFalse(self.live())
        self.solve.assert_not_called()
        self.establish()
        self.send(21, subtype=4, prefix='MSG', position=False, track=180, speed=450)
        self.assertFalse(self.live())
        self.assertIn('POSITION_AGE_GT_10', self.events[-1]['triggering_details']['motion_freshness']['reason_codes'])

    def test_fresh_position_but_old_altitude_does_not_get_velocity_grace(self):
        self.establish()
        old = r.aircraft_motion_states[ICAO].altitude
        r.aircraft_motion_states[ICAO].altitude = r.MotionParameter(old.value, NOW, old.source)
        self.send(11, subtype=4, prefix='MSG', position=False)
        self.assertFalse(self.live())
        self.assertIn('ALTITUDE_AGE_GT_10', self.events[-1]['triggering_details']['motion_freshness']['reason_codes'])

    def test_supported_late_message_timestamp_cannot_restart_hold(self):
        self.establish()
        self.send(11)
        self.assertTrue(self.live())
        # Real parser accepts measurement time independently of later receipt time.
        self.send(-20, subtype=4, prefix='MSG', position=False, track=180, speed=450, logged=11.5)
        self.assertFalse(self.live())
        self.assertEqual(NOW-datetime.timedelta(seconds=20), r.aircraft_motion_states[ICAO].track.updated_at_utc)
        reasons = self.events[-1]['triggering_details']['motion_freshness']['reason_codes']
        self.assertIn('POSITION_TRACK_DELTA_GT_10', reasons)

    def test_recovery_after_early_withdrawal_reaches_one_passed_event_per_body(self):
        self.target = NOW + datetime.timedelta(seconds=140)
        self.establish()
        self.send(11)
        self.send(20)  # T-120: existing EARLY_WITHDRAWAL policy is unchanged.
        self.assertFalse(self.live())
        self.assertEqual([], self.dashboard.state.query_history()['records'])
        self.assertTrue(all(e['history_reason'] == 'EARLY_WITHDRAWAL' for e in self.events))
        self.send(20.1, subtype=4, prefix='MSG', position=False, track=180, speed=450)
        self.assertTrue(self.live())
        for t in range(21, 140):
            self.send(t, track=180, speed=450)
        self.dashboard.tick(self.target)
        self.dashboard.tick(self.target+datetime.timedelta(seconds=1))
        history = self.dashboard.state.query_history()['records']
        self.assertEqual(2, len(history))
        self.assertEqual(['PASSED', 'PASSED'], [e['final_state'] for e in self.events[-2:]])

    def test_hold_through_t0_finalizes_once_without_new_prediction(self):
        self.target = NOW + datetime.timedelta(seconds=12)
        self.establish()
        self.send(11)
        self.solve.reset_mock()
        self.dashboard.tick(self.target)
        self.send(12.5)
        self.send(13)
        self.solve.assert_not_called()
        self.assertEqual(2, len(self.dashboard.state.query_history()['records']))
        self.assertEqual(2, len(self.events))

    def test_source_reset_cannot_preserve_local_hold(self):
        self.establish()
        self.send(11)
        r.invalidate_observer_dependent_state(self.observer, reason='SOURCE_RESET')
        self.assertFalse(self.live())
        self.send(12)
        self.assertFalse(self.live())

    def test_local_stale_expiry_does_not_remove_remote_replacement(self):
        self.establish()
        candidate = self.dashboard.state._live['SUN'][ICAO]['candidate']
        owner = object()
        self.dashboard.state.publish(candidate, source_owner=owner)
        self.send(20)
        self.assertIs(owner, self.dashboard.state._live['SUN'][ICAO]['source_owner'])
        self.assertEqual(1, len(self.events))  # Only the locally owned MOON.

    def test_aircraft_disappearance_still_cleans_up(self):
        self.establish()
        r.clock.advance_to(NOW+datetime.timedelta(seconds=r.MAX_AGE_SECONDS+11))
        r.clean_dict()
        self.assertNotIn(ICAO, r.plane_dict)
        self.assertFalse(self.live())

    def test_beast_precision_hold_and_coarse_fallback_do_not_refresh_speed(self):
        from mlat_beast_track import decode_mlat_beast_tc19
        from test_mlat_beast_track import frame, velocity_message
        r.mlat_beast_enabled = True
        self.send(0, track=188, speed=450)
        decoded = decode_mlat_beast_tc19(frame(velocity_message(icao=int(ICAO, 16))))
        r.update_mlat_beast_track(decoded, NOW)
        self.assertEqual('MLAT_BEAST_TC19_FRESH', r.effective_track_parameter(ICAO).source)
        for t in range(1, 11):
            self.send(t, track=188)
        self.assertEqual('MLAT_BEAST_TC19_HELD', r.effective_track_parameter(ICAO).source)
        self.assertEqual(NOW+datetime.timedelta(seconds=10), r.effective_track_parameter(ICAO).updated_at_utc)
        self.solve.reset_mock()
        self.send(11, track=188)
        self.assertTrue(self.live())
        self.solve.assert_not_called()
        self.assertEqual(11., r.get_aircraft_motion_freshness_status(ICAO).groundspeed_age)
        self.send(11.5, track=192, speed=450)
        self.assertEqual('mlat', r.effective_track_parameter(ICAO).source)
        self.assertTrue(self.live())

    def test_precision_recovery_before_next_sbs_does_not_end_existing_grace(self):
        self.establish('track')
        self.send(11, speed=450)
        self.assertTrue(self.live())
        now = NOW+datetime.timedelta(seconds=11.2)
        r.clock.advance_to(now)
        r.raw_adsb_tracks[ICAO] = r.RawAdsbTrackState(180.1, now, 180.)
        r.clean_dict()
        self.assertTrue(self.live())
        self.assertEqual([], self.events)
        r.clock.advance_to(NOW+datetime.timedelta(seconds=15))
        r.clean_dict()
        self.assertFalse(self.live())


if __name__ == '__main__':
    unittest.main()
