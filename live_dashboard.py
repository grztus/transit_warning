"""Fail-open local web dashboard for already-computed transit predictions."""

from copy import deepcopy
from dataclasses import asdict, dataclass
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import parse_qs, urlsplit

from dashboard_history import (
    DEFAULT_PAGE_SIZE,
    DashboardHistoryStore,
    records_to_csv,
)


UTC = datetime.timezone.utc
DEFAULT_HISTORY_LIMIT = 100
WITHDRAW_HISTORY_GRACE_SECONDS = 3.0
DEFAULT_SEP_GREEN_MAX_DEG = 3.0
DEFAULT_SEP_YELLOW_MAX_DEG = 5.0
DEFAULT_SEP_VISIBLE_MAX_DEG = 7.0


def utc_text(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class DashboardCandidate:
    body: str
    icao: str
    callsign: str | None
    predicted_event_utc: datetime.datetime
    separation_deg: float
    body_azimuth_deg: float
    body_elevation_deg: float
    aircraft_elevation_deg: float
    distance_km: float | None
    last_prediction_update_utc: datetime.datetime
    telegram_range: bool


class DashboardState:
    """Thread-safe live queues and bounded, non-persistent event history."""

    def __init__(self, history_limit=DEFAULT_HISTORY_LIMIT,
                 sep_green_max_deg=DEFAULT_SEP_GREEN_MAX_DEG,
                 sep_yellow_max_deg=DEFAULT_SEP_YELLOW_MAX_DEG,
                 sep_visible_max_deg=DEFAULT_SEP_VISIBLE_MAX_DEG,
                 history_store=None):
        self.history_limit = int(history_limit)
        self.sep_green_max_deg = float(sep_green_max_deg)
        self.sep_yellow_max_deg = float(sep_yellow_max_deg)
        self.sep_visible_max_deg = float(sep_visible_max_deg)
        self.history_store = history_store
        self._live = {"SUN": {}, "MOON": {}}
        self._history = []
        self._generated_at_utc = None
        self._lock = threading.RLock()

    def publish(self, candidate):
        body = candidate.body.upper()
        key = candidate.icao.upper()
        if body not in self._live:
            return False
        with self._lock:
            existing = self._live[body].get(key)
            first_separation = (
                existing["first_separation_deg"] if existing is not None
                else candidate.separation_deg)
            minimum_separation = min(
                candidate.separation_deg,
                existing["minimum_separation_deg"] if existing is not None
                else candidate.separation_deg)
            self._live[body][key] = {
                "candidate": candidate,
                "first_separation_deg": first_separation,
                "minimum_separation_deg": minimum_separation,
                "first_seen_utc": (
                    existing["first_seen_utc"] if existing is not None
                    else candidate.last_prediction_update_utc),
                "history_worthy": (
                    existing["history_worthy"] if existing is not None
                    else False),
            }
        return True

    def mark_history_worthy(self, icao, body):
        with self._lock:
            item = self._live.get(body.upper(), {}).get(icao.upper())
            if item is None:
                return False
            item["history_worthy"] = True
        return True

    def withdraw(self, icao, body, now_utc):
        body = body.upper()
        with self._lock:
            item = self._live.get(body, {}).pop(icao.upper(), None)
            if item is None:
                return False
            predicted = item["candidate"].predicted_event_utc
            near_event = now_utc >= predicted - datetime.timedelta(
                seconds=WITHDRAW_HISTORY_GRACE_SECONDS)
            if item["history_worthy"] or near_event:
                self._to_history_locked(item, now_utc, "WITHDRAWN")
        return True

    def withdraw_aircraft(self, icao, now_utc):
        changed = False
        for body in ("SUN", "MOON"):
            changed = self.withdraw(icao, body, now_utc) or changed
        return changed

    def tick(self, now_utc):
        with self._lock:
            self._generated_at_utc = now_utc
            for body in ("SUN", "MOON"):
                due = [icao for icao, item in self._live[body].items()
                       if item["candidate"].predicted_event_utc <= now_utc]
                for icao in due:
                    item = self._live[body].pop(icao)
                    self._to_history_locked(item, now_utc, "PASSED")

    def _to_history_locked(self, item, recorded_at_utc, reason):
        candidate = item["candidate"]
        event_id = "{}:{}:{}".format(
            candidate.icao.upper(), candidate.body.upper(),
            utc_text(candidate.predicted_event_utc))
        if any(event["event_id"] == event_id for event in self._history):
            return
        record = self._candidate_dict(candidate)
        record.update({
            "event_id": event_id,
            "final_separation_deg": candidate.separation_deg,
            "first_separation_deg": item["first_separation_deg"],
            "minimum_separation_deg": item["minimum_separation_deg"],
            "first_seen_utc": utc_text(item["first_seen_utc"]),
            "last_seen_utc": utc_text(
                candidate.last_prediction_update_utc),
            "history_recorded_at_utc": utc_text(recorded_at_utc),
            "outcome": reason,
        })
        self._history.insert(0, record)
        del self._history[self.history_limit:]
        if self.history_store is not None:
            try:
                self.history_store.append(record)
            except Exception:
                pass

    def query_history(self, utc_date=None, callsign=None, body="ALL",
                      offset=0, limit=DEFAULT_PAGE_SIZE):
        if self.history_store is not None:
            return self.history_store.query(
                utc_date, callsign, body, offset, limit)
        body = str(body or "ALL").upper()
        records = [record for record in self._history
                   if (utc_date is None or str(record.get(
                       "predicted_event_utc") or "").startswith(utc_date))
                   and (not callsign or callsign.casefold() in str(
                       record.get("callsign") or "").casefold())
                   and (body == "ALL" or str(
                       record.get("body") or "").upper() == body)]
        offset = max(0, int(offset))
        limit = max(1, min(100, int(limit)))
        page = records[offset:offset + limit]
        has_more = offset + limit < len(records)
        return {
            "records": deepcopy(page), "offset": offset, "limit": limit,
            "next_offset": offset + len(page) if has_more else None,
            "has_more": has_more,
        }

    def export_history_csv(self, utc_date=None, callsign=None, body="ALL"):
        if self.history_store is not None:
            return self.history_store.export_csv(utc_date, callsign, body)
        return records_to_csv(self.query_history(
            utc_date, callsign, body, 0, 100)["records"])

    def snapshot(self, now_utc=None):
        with self._lock:
            generated_at = self._generated_at_utc or now_utc
            result = {
                "generated_at_utc": (
                    utc_text(generated_at) if generated_at is not None
                    else None),
                "sun": {"candidates": self._body_snapshot_locked("SUN")},
                "moon": {"candidates": self._body_snapshot_locked("MOON")},
                "recent_events": deepcopy(self._history),
                "presentation": {
                    "sep_green_max_deg": self.sep_green_max_deg,
                    "sep_yellow_max_deg": self.sep_yellow_max_deg,
                    "sep_visible_max_deg": self.sep_visible_max_deg,
                },
            }
        return result

    def _body_snapshot_locked(self, body):
        items = sorted(
            self._live[body].values(),
            key=lambda item: (
                item["candidate"].predicted_event_utc,
                item["candidate"].icao),
        )
        return [self._candidate_dict(item["candidate"]) for item in items]

    def _candidate_dict(self, candidate):
        result = asdict(candidate)
        result["predicted_event_utc"] = utc_text(
            candidate.predicted_event_utc)
        result["last_prediction_update_utc"] = utc_text(
            candidate.last_prediction_update_utc)
        result["state"] = (
            "TELEGRAM RANGE" if candidate.telegram_range else "CANDIDATE")
        result["separation_class"] = self._separation_class(
            candidate.separation_deg)
        return result

    def _separation_class(self, separation_deg):
        separation = float(separation_deg)
        if separation < self.sep_green_max_deg:
            return "GREEN"
        if separation < self.sep_yellow_max_deg:
            return "YELLOW"
        if separation < self.sep_visible_max_deg:
            return "RED"
        return "HIDDEN"


class DisabledDashboard:
    def publish(self, candidate):
        return False

    def withdraw(self, icao, body, now_utc):
        return False

    def withdraw_aircraft(self, icao, now_utc):
        return False

    def mark_history_worthy(self, icao, body):
        return False

    def tick(self, now_utc):
        return None

    def close(self):
        return None


class DashboardRuntime:
    def __init__(self, state, server=None, thread=None):
        self.state = state
        self.server = server
        self.thread = thread

    def publish(self, candidate):
        return self.state.publish(candidate)

    def withdraw(self, icao, body, now_utc):
        return self.state.withdraw(icao, body, now_utc)

    def withdraw_aircraft(self, icao, now_utc):
        return self.state.withdraw_aircraft(icao, now_utc)

    def mark_history_worthy(self, icao, body):
        return self.state.mark_history_worthy(icao, body)

    def tick(self, now_utc):
        return self.state.tick(now_utc)

    def close(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=1.0)
        if self.state.history_store is not None:
            self.state.history_store.close()


def _handler_factory(state, now_utc):
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/api/state":
                self._send("application/json; charset=utf-8", json.dumps(
                    state.snapshot(now_utc()), separators=(",", ":"),
                ).encode("utf-8"))
            elif path == "/api/history":
                query = self._history_query(parsed.query)
                if query is None:
                    return
                body = json.dumps(
                    state.query_history(**query), separators=(",", ":")
                ).encode("utf-8")
                self._send("application/json; charset=utf-8", body)
            elif path == "/api/history/export.csv":
                query = self._history_query(parsed.query, export=True)
                if query is None:
                    return
                self._send(
                    "text/csv; charset=utf-8",
                    state.export_history_csv(**query),
                    disposition="attachment; filename=transit_history.csv")
            elif path in ("/", "/index.html"):
                self._send("text/html; charset=utf-8",
                           DASHBOARD_HTML.encode("utf-8"))
            else:
                self.send_error(404)

        def _history_query(self, raw_query, export=False):
            values = parse_qs(raw_query, keep_blank_values=True)
            date = values.get("date", [""])[0].strip() or None
            callsign = values.get("callsign", [""])[0].strip() or None
            body = values.get("body", ["ALL"])[0].strip().upper() or "ALL"
            if body not in ("ALL", "SUN", "MOON") or (
                    date is not None and (len(date) != 10
                                          or date[4] != "-"
                                          or date[7] != "-")):
                self.send_error(400, "Invalid history filter")
                return None
            if date is not None:
                try:
                    datetime.date.fromisoformat(date)
                except ValueError:
                    self.send_error(400, "Invalid history date")
                    return None
            result = {"utc_date": date, "callsign": callsign, "body": body}
            if not export:
                try:
                    result["offset"] = int(values.get("offset", ["0"])[0])
                    result["limit"] = int(values.get(
                        "limit", [str(DEFAULT_PAGE_SIZE)])[0])
                except ValueError:
                    self.send_error(400, "Invalid pagination")
                    return None
            return result

        def _send(self, content_type, body, disposition=None):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            if disposition is not None:
                self.send_header("Content-Disposition", disposition)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    return DashboardHandler


def start_dashboard(enabled, host, port, now_utc, error_handler=None,
                    server_factory=ThreadingHTTPServer,
                    sep_green_max_deg=DEFAULT_SEP_GREEN_MAX_DEG,
                    sep_yellow_max_deg=DEFAULT_SEP_YELLOW_MAX_DEG,
                    sep_visible_max_deg=DEFAULT_SEP_VISIBLE_MAX_DEG,
                    history_enabled=True,
                    history_dir="recordings/dashboard_history"):
    if not enabled:
        return DisabledDashboard()
    errors = error_handler or (lambda message: None)
    history_store = (
        DashboardHistoryStore(history_dir, errors)
        if history_enabled else None)
    state = DashboardState(
        sep_green_max_deg=sep_green_max_deg,
        sep_yellow_max_deg=sep_yellow_max_deg,
        sep_visible_max_deg=sep_visible_max_deg,
        history_store=history_store)
    try:
        state.tick(now_utc())
        server = server_factory(
            (host, port), _handler_factory(state, now_utc))
        thread = threading.Thread(
            target=server.serve_forever, name="transit-dashboard", daemon=True)
        thread.start()
        return DashboardRuntime(state, server, thread)
    except Exception as error:
        try:
            errors("Dashboard server failed: {}".format(type(error).__name__))
        except Exception:
            pass
        return DashboardRuntime(state)


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transit Warning</title>
<style>
:root{color-scheme:dark;font-family:system-ui,sans-serif;background:#101318;color:#eef}
body{margin:0;padding:12px;max-width:1100px;margin:auto}nav{display:flex;gap:8px;align-items:center;margin-bottom:12px}
button{padding:10px 18px;border:0;border-radius:8px;background:#273040;color:#fff}
button.active{background:#526b95}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.panel,.event{background:#191e27;border-radius:12px;padding:14px}.primary{border:1px solid #627ca9}
h1,h2,p{margin:4px 0}.countdown{font-size:2rem;font-variant-numeric:tabular-nums}
.candidate{border-top:1px solid #333;padding:10px 0}.muted{color:#9ba7ba}.hidden{display:none}
.sep{font-weight:750}.primary .sep{font-size:1.45rem;margin:7px 0}.sep.GREEN{color:#55d982}.sep.YELLOW{color:#f0c34d}.sep.RED{color:#ff6b6b}
.history-controls{display:flex;flex-wrap:wrap;gap:7px;margin:8px 0 12px}.history-controls input,.history-controls select{min-width:0;padding:8px;border:1px solid #3a4558;border-radius:7px;background:#11161e;color:#eef}.history-controls input[type=date]{flex:1 1 135px}.history-controls input[type=search]{flex:2 1 150px}.history-controls select{flex:1 1 80px}.history-actions{display:flex;gap:8px;margin-top:10px}.event{margin-top:8px}.event .sep{font-size:1.05rem}
.health{margin-left:auto;font-size:.8rem}.dot{display:inline-block;width:.65rem;height:.65rem;border-radius:50%;margin-right:.35rem}.active .dot{background:#36c66b}.stale .dot{background:#e0a62f}.disconnected .dot{background:#e45454}
@media(max-width:650px){.grid{grid-template-columns:1fr}}
</style></head><body>
<nav><button id="live-tab" class="active">LIVE</button><button id="history-tab">HISTORY</button><span id="health" class="health disconnected"><span class="dot"></span><span class="label">DISCONNECTED</span></span></nav>
<main id="live" class="grid"><section class="panel"><h1>☀️ SUN</h1><div id="sun"></div></section>
<section class="panel"><h1>🌙 MOON</h1><div id="moon"></div></section></main>
<main id="history" class="hidden"><section class="panel"><h1>HISTORY</h1><div class="history-controls"><input id="history-date" type="date" aria-label="UTC date"><input id="history-search" type="search" placeholder="Callsign" aria-label="Callsign search"><select id="history-body" aria-label="Celestial body"><option value="ALL">ALL</option><option value="SUN">SUN</option><option value="MOON">MOON</option></select></div><div id="events"></div><div class="history-actions"><button id="load-more" class="hidden">LOAD MORE</button><button id="export-csv">EXPORT CSV</button></div></section></main>
<script>
let state=null,failedPolls=0,historyRecords=[],historyOffset=0,historyHasMore=false;const STALE_AFTER_MS=10000,DISCONNECT_AFTER_FAILURES=2,HISTORY_PAGE_SIZE=25;const esc=s=>String(s??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function healthStatus(now=Date.now()){if(failedPolls>=DISCONNECT_AFTER_FAILURES)return'DISCONNECTED';let t=Date.parse(state?.generated_at_utc);return Number.isFinite(t)&&now-t<=STALE_AFTER_MS?'ACTIVE':'STALE'}
function renderHealth(){let status=healthStatus(),root=document.getElementById('health');root.className=`health ${status.toLowerCase()}`;root.querySelector('.label').textContent=status}
function countdown(utc){let s=Math.max(0,Math.floor((Date.parse(utc)-Date.now())/1000));return `${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`}
function eventTime(utc){return String(utc||'').slice(11,19)+' UTC'}
function sepClass(value){return ['GREEN','YELLOW','RED'].includes(value)?value:''}
function renderBody(name){let list=state?.[name]?.candidates||[],root=document.getElementById(name);if(!list.length){root.innerHTML='<p class="muted">No candidates</p>';return}
root.innerHTML=list.slice(0,3).map((c,i)=>`<article class="candidate ${i?'':'primary'}"><div class="countdown" data-utc="${esc(c.predicted_event_utc)}">${countdown(c.predicted_event_utc)}</div><h2>${esc(c.callsign||c.icao)}</h2><p class="sep ${sepClass(c.separation_class)}">SEP ${c.separation_deg.toFixed(2)}°</p><p>${esc(c.state)}</p>${i?'':`<p>AZ ${c.body_azimuth_deg.toFixed(1)}° · ALT ${c.body_elevation_deg.toFixed(1)}°</p><p>Aircraft ALT ${c.aircraft_elevation_deg.toFixed(1)}° · ${c.distance_km?.toFixed(0)??'—'} km</p><p class="muted">${eventTime(c.predicted_event_utc)}</p>`}</article>`).join('')}
function renderHistory(){document.getElementById('events').innerHTML=historyRecords.map(x=>`<article class="event"><b>${esc(x.callsign||x.icao)} · ${esc(x.body)} · ${esc(x.outcome)}</b><p>${esc(x.predicted_event_utc)}</p><p class="sep">Final SEP ${Number(x.final_separation_deg).toFixed(2)}°</p></article>`).join('')||'<p class="muted">No history events</p>';document.getElementById('load-more').classList.toggle('hidden',!historyHasMore)}
function historyQuery(offset=0){let q=new URLSearchParams({offset:String(offset),limit:String(HISTORY_PAGE_SIZE),body:document.getElementById('history-body').value}),d=document.getElementById('history-date').value,s=document.getElementById('history-search').value.trim();if(d)q.set('date',d);if(s)q.set('callsign',s);return q}
async function loadHistory(reset=true){let offset=reset?0:historyOffset,response=await fetch('/api/history?'+historyQuery(offset),{cache:'no-store'});if(!response.ok)return;let page=await response.json();historyRecords=reset?page.records:historyRecords.concat(page.records);historyOffset=page.next_offset??historyRecords.length;historyHasMore=page.has_more;renderHistory()}
function render(){renderBody('sun');renderBody('moon')}
async function refresh(){let controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),2500);try{let response=await fetch('/api/state',{cache:'no-store',signal:controller.signal});if(!response.ok)throw Error('HTTP');state=await response.json();failedPolls=0;render();renderHealth()}catch(e){failedPolls++;renderHealth()}finally{clearTimeout(timeout)}}
setInterval(()=>{document.querySelectorAll('[data-utc]').forEach(x=>x.textContent=countdown(x.dataset.utc));renderHealth()},1000);setInterval(refresh,3000);refresh();
document.getElementById('live-tab').onclick=()=>{document.getElementById('live').classList.remove('hidden');document.getElementById('history').classList.add('hidden')};
document.getElementById('history-tab').onclick=()=>{document.getElementById('history').classList.remove('hidden');document.getElementById('live').classList.add('hidden');loadHistory(true)};
for(let id of ['history-date','history-search','history-body'])document.getElementById(id).onchange=()=>loadHistory(true);
document.getElementById('load-more').onclick=()=>loadHistory(false);
document.getElementById('export-csv').onclick=()=>{location.href='/api/history/export.csv?'+historyQuery(0)};
</script></body></html>"""
