"""Small web UI: Garmin login, detected pairs, simulate / merge buttons."""
from __future__ import annotations

import html
import json
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import __version__
from .config import Config
from .garmin import GarminAuthRequired, GarminSession
from .service import Service
from .preview import summarize
from .store import Store
from .zwift import ZwiftClient

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "detected": "Detected",
    "running": "Running…",
    "simulated": "Simulated",
    "merged": "Merged",
    "failed": "Failed",
    "ignored": "Ignored",
}

CSS = """
:root { --bg:#f7f7f8; --fg:#1d1d1f; --muted:#6b6b70; --card:#fff; --line:#e3e3e6;
        --accent:#fc6719; --ok:#1f8a4c; --err:#c0392b; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#151517; --fg:#ececf0; --muted:#9a9aa2; --card:#1f1f23; --line:#2e2e34; }
}
* { box-sizing:border-box; }
body { margin:0; font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; background:var(--bg); color:var(--fg); }
main { max-width:960px; margin:0 auto; padding:24px 16px; }
h1 { font-size:22px; margin:0 0 4px; } .sub { color:var(--muted); margin:0 0 20px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:16px; }
table { width:100%; border-collapse:collapse; } th,td { text-align:left; padding:8px 6px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:500; font-size:13px; }
.msg { color:var(--muted); font-size:13px; } .st-merged { color:var(--ok); } .st-failed { color:var(--err); }
form.inline { display:inline; } button { font:inherit; padding:6px 12px; border-radius:6px; border:1px solid var(--line);
  background:var(--card); color:var(--fg); cursor:pointer; } button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
input { font:inherit; padding:6px 8px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg); }
a { color:var(--accent); }
.row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; } .wrap { overflow-x:auto; }
"""


def _page(body: str, head: str = "") -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>zwift-map-merge</title><style>{CSS}</style>{head}</head><body><main>"
        "<h1>zwift-map-merge</h1><p class='sub'>Adds the Zwift map to your watch activity "
        f"in Garmin Connect · v{__version__}</p>{body}</main></body></html>"
    )


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    garmin = GarminSession(cfg.token_dir)
    store = Store(cfg.db_path)
    service = Service(cfg, garmin, store)
    app = FastAPI(title="zwift-map-merge")
    app.state.service = service
    zwift = ZwiftClient(cfg.zwift_token_file)
    app.state.zwift = zwift
    zwift_lock = threading.Lock()
    security = HTTPBasic(auto_error=False)
    state = {"mfa_pending": False, "last_error": None, "upload_results": [],
             "zwift": {"running": False, "counts": None, "last": None, "error": None,
                       "login_error": None, "since": None, "finished_at": None}}

    def check_auth(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
        if not cfg.app_password:
            return
        if creds is None or not secrets.compare_digest(creds.password.encode(), cfg.app_password.encode()):
            raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})

    def run_bg(target_id: int, fn) -> None:
        store.set_status(target_id, "running", None)

        def job():
            try:
                fn(target_id)
            except Exception:  # noqa: BLE001 - status already recorded
                log.exception("Action on %s failed", target_id)
        threading.Thread(target=job, daemon=True).start()

    def zwift_sync(since: datetime) -> None:
        """Run a Zwift history sync; only one at a time."""
        zs = state["zwift"]
        if not zwift_lock.acquire(blocking=False):
            return
        zs.update(running=True, error=None, since=since.date().isoformat(), counts=None, last=None)

        def progress(counts, message):
            zs.update(counts=dict(counts), last=message)
        try:
            zs["counts"] = service.sync_zwift(zwift, since, progress)
        except Exception as e:  # noqa: BLE001
            log.exception("Zwift sync failed")
            zs["error"] = str(e)
        finally:
            zs["running"] = False
            zs["finished_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            zwift_lock.release()

    def scheduler(stop: threading.Event) -> None:
        if not garmin.has_tokens() and cfg.garmin_email and cfg.garmin_password:
            try:
                if garmin.start_login(cfg.garmin_email, cfg.garmin_password):
                    state["mfa_pending"] = True
            except Exception as e:  # noqa: BLE001
                log.warning("Automatic Garmin login failed: %s", e)
        if not zwift.connected() and cfg.zwift_email and cfg.zwift_password:
            try:
                zwift.login(cfg.zwift_email, cfg.zwift_password)
            except Exception as e:  # noqa: BLE001
                log.warning("Automatic Zwift login failed: %s", e)
        while not stop.is_set():
            if garmin.has_tokens():
                try:
                    service.scan()
                    if zwift.connected() and cfg.zwift_auto_sync:
                        zwift_sync(datetime.now(timezone.utc) - timedelta(days=cfg.scan_days))
                    service.merge_pending()
                    state["last_error"] = None
                except Exception as e:  # noqa: BLE001
                    log.exception("Scan failed")
                    state["last_error"] = str(e)
            stop.wait(cfg.scan_interval_minutes * 60)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop_event = threading.Event()
        threading.Thread(target=scheduler, args=(stop_event,), daemon=True).start()
        yield
        stop_event.set()

    app.router.lifespan_context = lifespan

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "garmin": garmin.has_tokens()}

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
    def index() -> HTMLResponse:
        if state["mfa_pending"]:
            return _page(
                "<div class='card'><h2>Garmin verification code</h2>"
                "<form method='post' action='/mfa' class='row'><input name='code' autocomplete='one-time-code' required>"
                "<button class='primary'>Submit</button></form></div>")
        if not garmin.has_tokens():
            err = f"<p class='st-failed'>{html.escape(state['last_error'])}</p>" if state["last_error"] else ""
            return _page(
                "<div class='card'><h2>Garmin Connect login</h2>"
                "<p class='msg'>Your password is not stored: only the tokens are kept in "
                f"<code>{html.escape(str(cfg.token_dir))}</code>.</p>{err}"
                "<form method='post' action='/login' class='row'>"
                "<input name='email' type='email' placeholder='Email' required>"
                "<input name='password' type='password' placeholder='Password' required>"
                "<button class='primary'>Log in</button></form></div>")
        return _page(_dashboard(cfg, store, state, zwift.connected()))

    @app.post("/login", dependencies=[Depends(check_auth)])
    def login(email: str = Form(...), password: str = Form(...)) -> RedirectResponse:
        try:
            state["mfa_pending"] = garmin.start_login(email, password)
            state["last_error"] = None
        except Exception as e:  # noqa: BLE001
            state["last_error"] = f"Login refused: {e}"
        return RedirectResponse("/", status_code=303)

    @app.post("/mfa", dependencies=[Depends(check_auth)])
    def mfa(code: str = Form(...)) -> RedirectResponse:
        try:
            garmin.submit_mfa(code)
            state["mfa_pending"] = False
            state["last_error"] = None
        except Exception as e:  # noqa: BLE001
            state["last_error"] = f"Code refused: {e}"
        return RedirectResponse("/", status_code=303)

    @app.post("/logout", dependencies=[Depends(check_auth)])
    def logout() -> RedirectResponse:
        garmin.logout()
        return RedirectResponse("/", status_code=303)

    @app.post("/scan", dependencies=[Depends(check_auth)])
    def scan() -> RedirectResponse:
        try:
            service.scan()
            state["last_error"] = None
        except GarminAuthRequired:
            pass
        except Exception as e:  # noqa: BLE001
            state["last_error"] = str(e)
        return RedirectResponse("/", status_code=303)

    @app.post("/upload", dependencies=[Depends(check_auth)])
    async def upload(files: list[UploadFile] = File(...)) -> RedirectResponse:
        results = []
        for f in files[:200]:
            data = await f.read()
            try:
                results.append(service.add_source_file(f.filename or "file.fit", data))
            except GarminAuthRequired:
                results.append("Garmin login required")
                break
            except Exception as e:  # noqa: BLE001
                log.exception("Upload of %s failed", f.filename)
                results.append(f"{f.filename}: error ({e})")
        state["upload_results"] = results
        return RedirectResponse("/", status_code=303)

    @app.post("/zwift/login", dependencies=[Depends(check_auth)])
    def zwift_login(email: str = Form(...), password: str = Form(...)) -> RedirectResponse:
        try:
            zwift.login(email, password)
            state["zwift"]["login_error"] = None
        except Exception as e:  # noqa: BLE001
            state["zwift"]["login_error"] = f"Zwift login refused: {e}"
        return RedirectResponse("/", status_code=303)

    @app.post("/zwift/logout", dependencies=[Depends(check_auth)])
    def zwift_logout() -> RedirectResponse:
        zwift.logout()
        return RedirectResponse("/", status_code=303)

    @app.post("/zwift/sync", dependencies=[Depends(check_auth)])
    def zwift_sync_route(since: str = Form(...)) -> RedirectResponse:
        try:
            start = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(400, "invalid date")
        if zwift.connected() and garmin.has_tokens():
            threading.Thread(target=zwift_sync, args=(start,), daemon=True).start()
        return RedirectResponse("/", status_code=303)

    @app.post("/pairs/{target_id}/{action}", dependencies=[Depends(check_auth)])
    def act(target_id: int, action: str) -> RedirectResponse:
        if not store.get(target_id):
            raise HTTPException(404)
        if action == "simulate":
            run_bg(target_id, service.simulate)
        elif action == "merge":
            run_bg(target_id, service.merge)
        elif action == "ignore":
            service.ignore(target_id)
        else:
            raise HTTPException(404)
        return RedirectResponse("/", status_code=303)

    @app.get("/pairs/{target_id}/merged.fit", dependencies=[Depends(check_auth)])
    def download(target_id: int) -> FileResponse:
        path = cfg.backup_dir / str(target_id) / f"{target_id}_merged.fit"
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, filename=f"{target_id}_merged.fit", media_type="application/octet-stream")

    @app.get("/pairs/{target_id}/preview", response_class=HTMLResponse, dependencies=[Depends(check_auth)])
    def preview(target_id: int) -> HTMLResponse:
        row = store.get(target_id)
        if not row:
            raise HTTPException(404)
        path = cfg.backup_dir / str(target_id) / f"{target_id}_merged.fit"
        if not path.exists():
            return _page("<div class='card'><p>No merged file yet: click <b>Simulate</b> first.</p>"
                         "<p><a href='/'>← Back</a></p></div>")
        return _page(*_preview_page(cfg, row, summarize(path.read_bytes())))

    return app


def _svg_chart(name: str, points: list[tuple[float, float]], invert: bool = False) -> str:
    """Small line chart as inline SVG (x = minutes)."""
    w, h, pad = 640, 120, 4
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs), max(xs) or 1
    y0, y1 = min(ys), max(ys)
    if y1 - y0 < 1e-9:
        y1 = y0 + 1
    def px(x, y):
        fx = pad + (x - x0) / ((x1 - x0) or 1) * (w - 2 * pad)
        fy = (y - y0) / (y1 - y0)
        fy = fy if invert else 1 - fy
        return f"{fx:.1f},{pad + fy * (h - 2 * pad):.1f}"
    line = " ".join(px(x, y) for x, y in points)
    fmt = (lambda v: f"{int(v)}:{int(v % 1 * 60):02d}") if name.startswith("Pace") else (lambda v: f"{v:.0f}")
    top, bottom = (y0, y1) if invert else (y1, y0)
    return (
        f"<div class='chart'><div class='row' style='justify-content:space-between'><b>{html.escape(name)}</b>"
        f"<span class='msg'>{fmt(bottom)} – {fmt(top)}</span></div>"
        f"<svg viewBox='0 0 {w} {h}' preserveAspectRatio='none' role='img' aria-label='{html.escape(name)}'>"
        f"<polyline points='{line}' fill='none' stroke='var(--accent)' stroke-width='1.5' "
        "vector-effect='non-scaling-stroke'/></svg>"
        f"<div class='row msg' style='justify-content:space-between'><span>0 min</span>"
        f"<span>{x1:.0f} min</span></div></div>"
    )


def _preview_page(cfg: Config, row: dict, pv) -> tuple[str, str]:
    tid = row["target_id"]
    merged = row["status"] == "merged"
    stats = "".join(
        f"<div class='stat'><span class='msg'>{html.escape(k)}</span><b>{html.escape(v)}</b></div>"
        for k, v in pv.stats.items())
    charts = "".join(_svg_chart(name, pts, invert=name.startswith("Pace")) for name, pts in pv.series.items())
    if merged:
        what = (f"This activity is on Garmin: <a href='https://connect.garmin.com/modern/activity/"
                f"{row['new_id']}' target='_blank' rel='noopener'>#{row['new_id']}</a>.")
    else:
        deleted = f"watch activity #{tid}"
        if row["source_id"] and cfg.delete_source:
            deleted += f" and the Zwift copy #{row['source_id']}"
        what = (f"Nothing has changed on Garmin yet. <b>Merge</b> will delete the {deleted}, import this file, "
                f"then restore the name “{html.escape(row['target_name'] or '')}”, description, type and gear.")
    track_json = json.dumps(pv.track)
    started = f"{pv.start:%Y-%m-%d %H:%M} UTC" if pv.start else ""
    body = (
        "<p><a href='/'>← Back</a></p>"
        f"<div class='card'><h2 style='margin:0'>{html.escape(row['target_name'] or 'Activity')}</h2>"
        f"<p class='msg' style='margin:4px 0 0'>{html.escape(pv.sport)} · {started} · "
        f"map from {html.escape(row['source_name'] or 'Zwift')}</p>"
        f"<p>{what}</p>"
        + (f"<div class='row'><form class='inline' method='post' action='/pairs/{tid}/merge' "
           "onsubmit=\"return confirm('Delete the activities on Garmin and import the merged one?')\">"
           "<button class='primary'>Merge</button></form>"
           f"<a href='/pairs/{tid}/merged.fit'>Download merged FIT</a></div>" if not merged else "")
        + "</div>"
        "<div class='card'><div id='map'></div>"
        "<p class='msg'>Zwift worlds are mapped onto real coordinates (Watopia sits in the Solomon Islands), "
        "so the track is drawn over that real place, as Garmin will show it.</p></div>"
        f"<div class='card'><div class='stats'>{stats}</div></div>"
        f"<div class='card'>{charts or '<p class=msg>No data series.</p>'}</div>"
        "<script>"
        f"const track = {track_json};"
        "if (track.length && window.L) {"
        " const map = L.map('map', {scrollWheelZoom: false});"
        " L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',"
        "  {maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'}).addTo(map);"
        " const line = L.polyline(track, {color: '#fc6719', weight: 4}).addTo(map);"
        " L.circleMarker(track[0], {radius: 6, color: '#1f8a4c', fillOpacity: 1}).addTo(map);"
        " L.circleMarker(track[track.length - 1], {radius: 6, color: '#c0392b', fillOpacity: 1}).addTo(map);"
        " map.fitBounds(line.getBounds(), {padding: [20, 20]});"
        "} else { document.getElementById('map').innerHTML = '<p class=msg>No GPS track.</p>'; }"
        "</script>"
    )
    head = (
        "<link rel='stylesheet' href='https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css'>"
        "<script src='https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js'></script>"
        "<style>#map{height:420px;border-radius:8px}.stats{display:grid;"
        "grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px}.stat{display:flex;flex-direction:column}"
        ".chart{margin-bottom:16px}.chart svg{width:100%;height:120px;display:block;"
        "background:var(--bg);border-radius:6px}</style>"
    )
    return body, head


def _zwift_card(cfg: Config, state: dict, connected: bool) -> str:
    zs = state["zwift"]
    err = f"<p class='st-failed'>{html.escape(zs['login_error'])}</p>" if zs["login_error"] else ""
    if not connected:
        return (
            "<div class='card'><b>Zwift account</b>"
            "<p class='msg'>Connect Zwift to fetch your Zwift FIT files automatically, including your "
            "whole history. This uses Zwift's unofficial Companion API. Your password is not stored, "
            "only the tokens.</p>" + err +
            "<form method='post' action='/zwift/login' class='row'>"
            "<input name='email' type='email' placeholder='Zwift email' required>"
            "<input name='password' type='password' placeholder='Zwift password' required>"
            "<button class='primary'>Connect Zwift</button></form></div>")
    if zs["running"]:
        c = zs["counts"] or {}
        status = (f"<p>Syncing since {html.escape(zs['since'] or '')}… "
                  f"{c.get('seen', 0)} Zwift activities looked at, {c.get('downloaded', 0)} downloaded, "
                  f"{c.get('paired', 0)} paired. <a href='/'>Refresh</a></p>")
    elif zs["counts"] is not None or zs["error"]:
        c = zs["counts"] or {}
        status = (f"<p class='msg'>Last sync ({html.escape(zs['finished_at'] or '')}, since "
                  f"{html.escape(zs['since'] or '')}): {c.get('seen', 0)} looked at, "
                  f"{c.get('downloaded', 0)} downloaded, {c.get('paired', 0)} paired, "
                  f"{c.get('errors', 0)} errors.</p>")
        if zs["error"]:
            status += f"<p class='st-failed'>{html.escape(zs['error'])}</p>"
    else:
        status = ""
    last = f"<p class='msg'>{html.escape(zs['last'])}</p>" if zs["last"] else ""
    auto = (f"New Zwift activities are fetched at each scan ({cfg.scan_days} days)."
            if cfg.zwift_auto_sync else "Automatic fetch is off (ZWIFT_AUTO_SYNC=false).")
    return (
        "<div class='card'><div class='row' style='justify-content:space-between'><b>Zwift account: connected</b>"
        "<form class='inline' method='post' action='/zwift/logout'><button>Disconnect Zwift</button></form></div>"
        f"<p class='msg'>{auto} To catch up on older activities, sync the history from a date. Only Zwift "
        "activities that overlap a watch activity without a map are downloaded; the others are skipped. "
        "Pairs found show up below: nothing changes on Garmin until you merge them.</p>"
        "<form method='post' action='/zwift/sync' class='row'>"
        "<label>Since <input type='date' name='since' required value='2020-01-01'></label>"
        f"<button class='primary' {'disabled' if zs['running'] else ''}>Sync history</button></form>"
        + status + last + "</div>")


def _dashboard(cfg: Config, store: Store, state: dict, zwift_connected: bool = False) -> str:
    rows = store.all()
    mode = ("<b>automatic</b>: every detected pair is merged" if cfg.auto_merge
            else "<b>manual</b>: nothing changes until you click “Merge”")
    err = f"<p class='st-failed'>Last error: {html.escape(state['last_error'])}</p>" if state["last_error"] else ""
    head = (
        "<div class='card'><div class='row' style='justify-content:space-between'>"
        f"<div>Mode {mode}. Scans every {cfg.scan_interval_minutes} min over the last {cfg.scan_days} days.</div>"
        "<div class='row'><form class='inline' method='post' action='/scan'><button>Scan now</button></form>"
        "<form class='inline' method='post' action='/logout'><button>Disconnect Garmin</button></form></div>"
        f"</div>{err}</div>"
    )
    head += _zwift_card(cfg, state, zwift_connected)
    results = state.pop("upload_results", None) or []
    state["upload_results"] = []
    res_html = "".join(f"<li>{html.escape(r)}</li>" for r in results)
    head += (
        "<div class='card'><b>Older activities</b>"
        "<p class='msg'>Zwift copy no longer on Garmin? Upload its FIT files here, downloaded from "
        "zwift.com (activity → “Download .fit”) or taken from your Strava data export "
        "(<code>activities/</code> folder, <code>.fit.gz</code> accepted). Each file is paired with "
        "the watch activity without a map that overlaps it.</p>"
        "<form method='post' action='/upload' enctype='multipart/form-data' class='row'>"
        "<input type='file' name='files' accept='.fit,.gz' multiple required>"
        "<button>Upload</button></form>"
        + (f"<ul class='msg'>{res_html}</ul>" if res_html else "")
        + "</div>"
    )
    if not rows:
        return head + "<div class='card'><p class='msg'>No watch + Zwift pair found yet.</p></div>"
    lines = []
    for r in rows:
        tid = r["target_id"]
        status = r["status"]
        actions = ""
        if status in ("detected", "simulated", "failed"):
            actions = (
                f"<form class='inline' method='post' action='/pairs/{tid}/simulate'><button>Simulate</button></form> "
                f"<form class='inline' method='post' action='/pairs/{tid}/merge' "
                "onsubmit=\"return confirm('Delete both activities on Garmin and import the merged one?')\">"
                "<button class='primary'>Merge</button></form> "
                f"<form class='inline' method='post' action='/pairs/{tid}/ignore'><button>Ignore</button></form>"
            )
        if status in ("simulated", "merged"):
            actions += f" <a href='/pairs/{tid}/preview'>Preview</a> · <a href='/pairs/{tid}/merged.fit'>FIT</a>"
        new = (f"<br><a href='https://connect.garmin.com/modern/activity/{r['new_id']}' target='_blank' rel='noopener'>"
               "View on Garmin</a>") if r["new_id"] else ""
        lines.append(
            "<tr>"
            f"<td>{html.escape((r['start_utc'] or '')[:16].replace('T', ' '))} UTC</td>"
            f"<td>{html.escape(r['target_name'] or '')}<br><span class='msg'>#{tid}</span></td>"
            f"<td>{html.escape(r['source_name'] or '')}"
            + (f"<br><span class='msg'>#{r['source_id']}</span>" if r["source_id"] else "")
            + "</td>"
            f"<td class='st-{status}'>{STATUS_LABELS.get(status, status)}{new}"
            f"<div class='msg'>{html.escape(r['message'] or '')}</div></td>"
            f"<td>{actions}</td></tr>"
        )
    return head + (
        "<div class='card wrap'><table><tr><th>Start</th><th>Watch</th><th>Zwift</th><th>Status</th><th></th></tr>"
        + "".join(lines) + "</table></div>"
    )


def run(host: str, port: int) -> None:
    import uvicorn
    uvicorn.run(create_app(), host=host, port=port)
