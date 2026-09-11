# STANDARD aircraft sources

AIRCRAFT_SOURCE_MODE selects the startup mode: LOCAL (default), INTERNET,
or AUTO. The runtime settings API accepts
{"aircraft_source":{"requested_mode":"AUTO"}} inside the existing revisioned
settings command. Restart restores the configured mode. The frontend is unchanged.

LOCAL preserves existing SBS/RAW/MLAT prediction and recording. INTERNET ignores
local prediction updates and uses the shared TRUE_2D solver with EGM96 conversion
and pressure-zero topocentric ephemerides. Missing geoid data produces ERROR;
there is no per-event LEGACY fallback. Remote notification integration is not
part of this checkpoint.

One provider worker publishes a replaceable snapshot mailbox. HTTP acquisition
does not hold runtime source or aircraft locks. Switching sources invalidates
old candidates and clears local/fusion input state. Private bridge ownership
prevents delayed remote withdrawal from deleting a LOCAL replacement.

STATIC and MANUAL can supply an explicit query centre. Requested MOBILE,
including static fallback, blocks provider HTTP. Observer-scope changes discard
old mailbox evidence. Provider clock offsets are diagnostic only: position expiry
uses relative provider age and monotonic elapsed time, not cross-clock subtraction.
Repeated evidence cannot renew its age. Other remote field ages remain UNKNOWN.

AUTO keeps local ingest active and selects fields independently. Fresh/degraded
known-age local fields take priority; fresh/held precision track retains its
existing preference. Remote fallback requires eligible position evidence
(20 seconds) and a live snapshot lease (30 seconds). Unknown-age remote
vertical-rate and intent fields remain diagnostic; remote-dependent predictions
use frozen altitude. Pressure altitude and WGS84 HAE stay distinct, and predictor
altitude records its QNH/geoid conversion lineage.

Candidate provenance is detached from mutable inputs. Ordinary live candidates
omit the forensic payload; completed history and CSV retain selected fields,
alternatives, ages, reasons, datum and the lossless JSON representation. Existing
LOCAL records remain readable. Snapshot final-revision binding is additive.
Provider data is not converted into synthetic receiver messages or written into
full-session receiver recordings.
