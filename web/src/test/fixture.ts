import type { BootstrapDto } from "../types";

export const activeFixture: BootstrapDto = {
  schema_version: 1,
  live_revision: 42,
  settings_revision: 3,
  generated_at_utc: "2026-09-04T10:00:00Z",
  health: "ACTIVE",
  observer: {
    requested_mode: "MOBILE",
    effective_source: "MOBILE",
    fallback_enabled: true,
    fallback_active: false,
    mobile_age_seconds: 2.4,
    mobile_accuracy_m: 6.2,
    gps_health: "ACTIVE",
  },
  bodies: {
    sun: {
      current_position: { altitude_deg: 31.2, azimuth_deg: 217.4, evaluated_at_utc: "2026-09-04T10:00:00Z" },
      candidates: [{
        body: "SUN", icao: "ABC123", callsign: "TEST123",
        predicted_event_utc: "2026-09-04T10:02:30Z", separation_deg: 0.42,
        encounter_id: "7:ABC123:SUN:2", prediction_geometry: "TRUE_2D",
        state: "ACTIVE", separation_class: "GREEN",
      }],
    },
    moon: {
      current_position: { altitude_deg: 9.1, azimuth_deg: 298.2 },
      candidates: [],
    },
  },
  recent_events: [{
    event_id: "event-public-1", body: "MOON", icao: "DEF456",
    callsign: "RECENT1", final_separation_deg: 0.8, outcome: "PASSED",
  }],
  presentation: { sep_green_max_deg: 3, sep_yellow_max_deg: 5, sep_visible_max_deg: 7 },
  capabilities: { runtime_settings: { telegram_sun_enabled: true } },
};
