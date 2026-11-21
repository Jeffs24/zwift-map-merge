"""Small web UI: Garmin login, detected pairs, simulate / merge buttons."""
from __future__ import annotations

import html
import logging
import secrets
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import __version__
from .config import Config
from .garmin import GarminAuthRequired, GarminSession
from .service import Service
from .store import Store

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
.row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; } .wrap { overflow-x:auto; }
"""


def _page(body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>zwift-map-merge</title><style>{CSS}</style></head><body><main>"
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
    security = HTTPBasic(auto_error=False)
    state = {"mfa_pending": False, "last_error": None, "upload_results": []}

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

    def scheduler(stop: threading.Event) -> None:
        if not garmin.has_tokens() and cfg.garmin_email and cfg.garmin_password:
            try:
                if garmin.start_login(cfg.garmin_email, cfg.garmin_password):
                    state["mfa_pending"] = True
            except Exception as e:  # noqa: BLE001
                log.warning("Automatic Garmin login failed: %s", e)
        while not stop.is_set():
            if garmin.has_tokens():
                try:
                    service.scan_and_maybe_merge()
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
        return _page(_dashboard(cfg, store, state))

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

    return app


def _dashboard(cfg: Config, store: Store, state: dict) -> str:
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
            actions += f" <a href='/pairs/{tid}/merged.fit'>Merged FIT</a>"
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
