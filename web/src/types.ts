export type BackendHealth = "ACTIVE" | "STALE";
export type BodyName = "SUN" | "MOON";

export interface BodyPositionDto {
  altitude_deg?: number;
  azimuth_deg?: number;
  evaluated_at_utc?: string;
}

export interface TransitCandidateDto {
  body?: string;
  icao?: string;
  callsign?: string | null;
  predicted_event_utc?: string;
  separation_deg?: number;
  body_azimuth_deg?: number;
  body_elevation_deg?: number;
  aircraft_elevation_deg?: number;
  distance_km?: number;
  transit_distance_km?: number;
  last_prediction_update_utc?: string;
  telegram_range?: boolean;
  encounter_id?: string;
  prediction_geometry?: string;
  state?: string;
  separation_class?: string;
  is_new_late_candidate?: boolean;
}

export interface RecentEventDto extends TransitCandidateDto {
  event_id?: string;
  final_separation_deg?: number;
  first_separation_deg?: number;
  minimum_separation_deg?: number;
  first_seen_utc?: string;
  last_seen_utc?: string;
  history_recorded_at_utc?: string;
  outcome?: string;
}

export interface BodyStateDto {
  current_position: BodyPositionDto | null;
  candidates: TransitCandidateDto[];
}

export interface ObserverStatusDto {
  requested_mode?: string;
  effective_source?: string;
  fallback_enabled?: boolean;
  fallback_active?: boolean;
  mobile_age_seconds?: number | null;
  mobile_accuracy_m?: number | null;
  gps_health?: string;
}

export interface PresentationDto {
  sep_green_max_deg?: number;
  sep_yellow_max_deg?: number;
  sep_visible_max_deg?: number;
}

export interface BootstrapDto {
  schema_version: 1;
  live_revision: number;
  settings_revision: number;
  generated_at_utc?: string | null;
  health: BackendHealth;
  observer: ObserverStatusDto;
  bodies: {
    sun: BodyStateDto;
    moon: BodyStateDto;
  };
  recent_events: RecentEventDto[];
  presentation: PresentationDto;
  settings: RuntimeSettingsValuesDto;
  capabilities: Record<string, unknown>;
}

export interface RuntimeSettingsValuesDto {
  telegram: { sun_enabled: boolean; moon_enabled: boolean };
  observer: { requested_mode: "STATIC" | "MOBILE"; fallback_enabled: boolean };
}

export interface LiveStateEvent {
  schema_version: 1;
  event: "live_state";
  live_revision: number;
  generated_at_utc?: string | null;
  payload: Pick<BootstrapDto, "generated_at_utc" | "bodies" | "recent_events" | "presentation">;
}

export interface SettingsSnapshotDto {
  schema_version: 1;
  revision: number;
  values: RuntimeSettingsValuesDto;
  capabilities: Record<string, unknown>;
  persistence: string;
}

export interface SettingsMutation {
  expected_revision: number;
  command_id: string;
  changes: {
    telegram?: Partial<RuntimeSettingsValuesDto["telegram"]>;
    observer?: Partial<RuntimeSettingsValuesDto["observer"]>;
  };
}

export interface SettingsEvent {
  schema_version: 1;
  event: "settings";
  settings_revision: number;
  payload: SettingsSnapshotDto;
}
