# Changelog

## 3.8.0 (unreleased)

- Fix: New print failed with "HTTP 413" for a model or a G-code file above about 1 MB. A proxy between the browser and the app (Home Assistant's ingress) refuses bigger request bodies, and the app's own limit is 100 MB. The page now sends models and G-code in parts of 512 KB and the app joins them (`total` and `offset` in the query of `POST /api/jobs`, `POST /api/jobs/{id}/model` (new) and `POST /api/jobs/{id}/gcode`, see API.md). A request without `total` works as before.

## 3.7.1 (unreleased)

- Fix: a printer that was online showed "Printer offline". The daemon asks the printer for a full push on every status call that finds its cache older than 10 s, and an idle printer sends no updates, so the calls queued on one thread and timed out.
- Bridge: `GET /api/status/<serial>` runs one daemon call at a time per printer, reuses an answer younger than 5 s (`DW_STATUS_CACHE`), waits 8 s for the daemon (`DW_STATUS_TIMEOUT`), and answers a failed call with the last good status up to 10 min old (`DW_STATUS_STALE`). Every answer has the header `X-Status-Age` (seconds). Only without a good status it answers 503.
- Hub: one failed status call no longer flips a printer offline. The printer view keeps the last good status and adds `stale` (older than 60 s) and `status_age` (seconds). A printer is offline when the bridge printer list says `online: false`, or there has been no status yet. The printer card shows "status from N min ago" when stale. The queue uses a kept status for 10 min at most.
## 3.7.0 (unreleased)

- The "Slice on the Pi" choice is gone. New print slices in the browser only.
- On iPhone and iPad, New print opens the app as a top-level page by itself, so the fast multi-thread slicer can run. Without isolation the single-thread engine runs, which is slower.
- New print accepts STL files only.
- Remove all server-side slicing. The app slices only in the browser, with the OrcaSlicer WebAssembly engine. Reason: the native slicer was 733 MB of the 967 MB aarch64 image, and the browser engine is as fast (CI test plate on an arm runner: native 7.0, 7.0 and 9.0 s per variant, WebAssembly with threads 8.8, 8.2 and 10.1 s).
- The image is much smaller: 248 MB instead of 967 MB on aarch64, 185 MB instead of 938 MB on amd64 (CI, `docker image ls`). The slicer, its flatpak runtime and the scripts that ran it are gone. The WebAssembly engine, ffmpeg, qemu and the bridge stay.
- `POST /api/jobs/{id}/slice` is gone and answers 404. The browser routes stay: `POST /api/jobs`, `GET /api/jobs/{id}`, `GET /api/jobs/{id}/model`, `POST /api/jobs/{id}/gcode`, the plate and G-code files, `POST /api/jobs/{id}/queue` and `POST /api/jobs/{id}/print`.
- The actions `druckwerk.slice` and `druckwerk.get_slice` and the LLM tools `druckwerk_slice` and `druckwerk_get_slice_results` are gone. An agent can no longer slice. It can only queue a sliced `.gcode.3mf` that is already in the share.
- `druckwerk.add_to_queue` and `druckwerk_add_to_queue` lose the field `slice_job_id`. No tool hands out a job id any more. `file` is now required. The LLM prompt says that the app has no slice tool.
- A job that an older version left in state `queued` or `slicing` is marked `failed` at start ("Slice again in the browser").
- Models that are not STL (3MF, STEP, OBJ) can no longer be sliced in the app. Slice them elsewhere and add the `.gcode.3mf` to the share.
- The CI image test checks that the image is at most 450 MB and that `/slice` answers 404. It stores a real Bambu Studio G-code through `/gcode` instead of slicing on the server.
- The Pi half of the slicing progress from 3.6.0 (step lines from the slicer log, `progress` on `GET /api/jobs/{id}` while the Pi slices) is gone with the Pi slicer. Browser slicing progress and the print start steps stay.

## 3.6.0 (unreleased)

- Progress from Start to the first layer. Slicing shows its percent and the slicer's step text, in the browser (the engine's progress messages) and on the Pi (step lines from the slicer log, `GET /api/jobs/{id}` has `progress` `{percent, message}`). After Start, a list of steps shows where the print is: upload to the Bambu cloud (percent), the printer downloads the plate (percent from `gcode_file_prepare_percent`), the printer prepares (stage name, such as heating the bed), then printing. The steps are on the printer card, the printer page, the queue row and the job page, and in the Start dialogs while the request runs. `GET /api/printers` adds `start` (the steps, or null) and `start_job` (the job being started, or null).
- The bridge reads the upload percent from the daemon's `print progress` log lines, so the daemon patch and the base image stay as they are. `GET /api/print-progress/<serial>` on the bridge answers `{stage, percent}` while a print is being sent, else `{}`.
## 3.5.0 (unreleased)

- Plates inbox. A cloud agent that cannot reach Home Assistant pushes a sliced plate to a private GitHub repo, and the app adds it to the print queue. Two new app options: `plates_repo` (default `marionebl/druckwerk-plates`) and `plates_token` (a GitHub token with Contents read and write on that repo). Without a token the feature is off and the app makes no request.
- Every 60 s the app lists `plates/` in the repo. Each `plates/<id>/<name>.gcode.3mf` goes through the same path as a plate queued from the web UI (sliced check, grams and print time, plate image) and also through the G-code guard and the printer model check, which now run at queue time. The job waits in the queue like any other. Nothing starts a print by itself.
- Target printer: `job.json` `printer` (serial), else `job.json` `model`, else the model in the G-code header, queued for any printer of that model. Owner: `job.json` `owner` (an HA user id or name) if that user may add jobs for the printer, else the printer's owner, else nobody (an admin job named "Plates inbox").
- After the job is queued the app deletes the files of the folder in the repo (one contents API DELETE per file). The taken ids are kept in `/data/plates_taken.json`, so a failed delete never makes a second job; the delete is tried again at the next poll.
- Files over 100 MB, files that are not sliced, and plates that fail the guard or the model check are logged, skipped and left in the repo. They are not downloaded again until the file changes. The token is never logged.
- Setup view: the last step shows the repo, the last poll time and the last error. `GET /api/setup` has `plates_inbox: {enabled, repo, last_poll, last_error, taken}`.
- How an agent pushes a plate is in API.md.

## 3.2.3 (unreleased)

- A camera snapshot no longer restarts the bridge daemon. Before, every "camera URL callback timed out" restarted it, so an automation that took a snapshot every 20 s kept the daemon down and a print start failed with 503 "daemon not reachable". The snapshot now answers 502 and the daemon keeps running.
- The protection stays, but only for a real stall. A camera timeout marks the printer. The bridge then sends a pushall through the daemon and waits up to 15 s (`DW_PROBE_WAIT`) for the cached status (`sequence_id` or `t_utc`) to change. It restarts the daemon only if the status does not change, or `/health` fails for 60 s. A printer that is idle sends no status by itself, so the bridge never judges by silence and never sends a pushall without a camera timeout. A timeout within 3 minutes of a daemon start (`DW_START_GRACE`) is ignored, because mints fail in that window anyway. At most one restart in 5 minutes (`DW_RESTART_BACKOFF`). It never restarts or probes while a print start or a command runs; it looks again at the next check (every 5 s).
- The log has one line for each timeout (seconds since the daemon start) and one for each probe result, for example `camera timeout probe: status answered in 2.3s, no restart`.
- Snapshot cache: a good snapshot younger than 30 s (`DW_SNAPSHOT_CACHE`) is sent again without asking the daemon. Requests for one printer wait for each other, so only one asks the daemon. `GET /api/camera/<serial>/snapshot` adds the header `X-Snapshot-Age` (seconds since the picture was taken, `0.0` for a new one). A failed request is also shared with the requests that waited for it.
- A print start (`POST /api/print`) and the command routes (light, rfid, pause, drying) wait up to 60 s (`DW_RETRY_WINDOW`) while the daemon is restarting (connection refused), then answer 503 as before. Other errors are not retried, because the daemon may have got the request.

## 3.2.2 (unreleased)

- Fix: starting a print failed with "read file: Error parsing `multipart/form-data` request" for files over 2 MB. The bridge (boo-cloud) used axum's default body limit of 2 MB on `POST /print`. The bridge patch now sets 64 MiB, the same cap as Druckwerk uploads. The patch has a test that posts a 3 MB file.

## 3.2.1 (unreleased)

- Deep links work in the Home Assistant panel. The app now uses the panel's own route messages (`home-assistant/subscribe-properties`, `home-assistant/properties`, `home-assistant/navigate`), so the HA address bar shows `/app/<slug>/history/123` and that URL opens the history item. The HA back button moves between app screens. Before, the app wrote its route into the hash of the HA page, which the panel does not keep.

## 3.2.0 (unreleased)

- One History. The History tab lists the Bambu Cloud tasks with the Druckwerk job merged in (name, owner, cost, options, slice). A finished job that no cloud task explains (never sent, cancelled before the start) is listed too and marked "Druckwerk only". The Queue no longer has a History segment. `GET /api/history` items gain `job`, `when` and `printer_owner`; `source` can be `job`.
- Every screen has a URL: `#/queue`, `#/history`, `#/history/<id>`, `#/queue/<job id>`, `#/filament`, `#/filament/<spool id>`, `#/printers/<serial>`, `#/new`, `#/access`. Reload opens the same view. Inside Home Assistant the app mirrors its route into the panel's URL hash and reads it back on load, so `<panel URL>#/history/123` is a link you can share.
- History detail: status, times, printer, grams, cost, the plate image (a placeholder box when it does not load), the print options and the slice, the 3D preview while the slicer still has the file, and Print again, Reslice and Retry where the source exists. Queue detail: the same, plus change name, options and target printer, approve, reject and cancel. `POST /api/queue/{id}/edit` is new. Jobs made from a slice keep `slice_job` and `slice`.
- Reslice opens New print with the same model and settings (`ctx.reslice`). New print prefills the model (from `GET /api/jobs/{id}/model`), name, options, printer, filament (library spool or AMS tray), build plate and the quality pick. A new slice is queued as a new job; the old job stays until you cancel it.
- Filament detail (`#/filament/<id>`): the spool and the prints done with it, linked to History. `GET /api/spools/{id}/prints` is new.
- Lists show "Loading…" until the first answer and "No x yet" only after an empty answer; a failure shows the error. Printers, filament, queue and history show the last answer from a per-user localStorage cache at once and replace it when the network answers; a change drops the cached list. Storage that is empty or blocked changes nothing.
- Ownership is a property of the printer: `PUT /api/setup/printers/{serial}` takes `owner` (an HA user), the Setup wizard has an Owner field. The `owner` role is gone from grants; at start owner grants move into the printer's `owner` and are dropped (see API.md). The owner is an admin of the printer, gets load requests and approval notifications, and is named in the History for prints nobody started here ("Not started here" stays when there is no owner).

## 3.1.1 (unreleased)

- No broken images on the printer cards and the printer page: the camera image stays hidden until a frame has loaded, and the placeholder says "Loading camera image…" or "No camera image". Before, the `<img>` had no source until the first snapshot arrived (10 to 60 s through the bridge, or never when the snapshot fails), so it showed its alt text. The plate image on New print is removed when it fails to load.
- An AMS whose slots are all empty has no tray row on the printer card and the printer page; they say "AMS empty". Units with at least one spool still show all their slots.
- New print, Filament: choose from the AMS trays ("In the AMS", the first loaded tray is selected) or from the spools of the printer's cloud account that are in no printer ("From your library"). With an empty AMS the library is the list and the page says "The AMS is empty." A library spool is sliced with the preset named like its brand and name, else Generic of its type. It is not loaded, so the page says so and offers "Ask to load" (load request). The queue job records the choice as `slice_spool`.
- `GET /api/engine/filament`, `POST /api/jobs/{id}/slice` and `POST /api/jobs/{id}/gcode` take `type`, `brand`, `name`, `colour`, `spool` instead of `tray`. `GET /api/printers` adds `light` (chamber light on, off, or null) and `account`.
- Chamber light switch on the printer card and the printer page: `POST /api/printers/{serial}/light` `{on}` (admin of the printer, as the other controls).

## 3.0.1 (unreleased)

- Make the app image about 70 % smaller, so updates download faster.
- Remove unused slicer files, the full qemu package and the full ffmpeg package.
- Split the image into a base image and the app code. The base has the slicer, qemu and ffmpeg and stays the same between versions, so later updates download only the app layers.

## 3.0.0 (unreleased)

- Druckwerk works on a Home Assistant without the vendor's printer integration (ha-bambulab). The app reads every printer's state from the bridge's MQTT status (`hub/printer_state.py`): status, stage, progress, remaining time, end time, layers, temperatures, task name, active tray, AMS units with trays, humidity and temperature, external spool and HMS errors. It does so on every install, with or without the vendor integration, so both behave the same. `GET /api/printers` keeps its shape.
- Printers register from the bridge's list (the accounts' bound printers). `sensor.<prefix>_print_status` is no longer required, and `PUT /api/setup/printers/{serial}` no longer checks it. `ha_prefix` is optional: it defaults to the vendor integration's prefix if HA has the printer, else to a name made from the printer's name. Saved printers keep their prefix.
- Changes you can see on an install with the vendor integration: the printer's online state is "the bridge answers", not the integration's online sensor; the AMS tray name is the tray's sub-brand or type from the printer; temperatures are whole degrees; HMS entries show the code, the severity and a link to the vendor's HMS page, but no error text; a stage the app does not know is `null`; printers that the bridge lists and HA has no entities for are registered too.
- `GET /api/printers/{serial}/cover` serves the plate image of the file Druckwerk sent for the running print (or the file named like the printer's task), else the cover of the cloud task with that name, else 404. `cover_url` is set while the printer reports a task name. It no longer reads `image.<prefix>_cover_image`.
- The chamber light state and the task name for the integration's image come from the bridge status, not from vendor entities. The air-print watchdog and the queue read the bridge too; the watchdog is always on.
- Integration 0.14.0: without the vendor integration it creates its own device per printer (identifiers `druckwerk`, serial); with it, entities attach to the vendor's device as before. Entity ids do not change.

## 2.9.0 (unreleased)

- Ask the printer owner to load a spool: `POST /api/spools/<id>/load-request`, `GET /api/load-requests`, `DELETE /api/load-requests/<id>`. The Filament tab has an "Ask to load" button per spool and shows open requests as a pill, so owners use it as a to-do list. The owners of the printer (not other HA admins) get a mobile notification when they are home; the `druckwerk_load_requested` bus event is fired. A request closes when the cloud library shows the spool in that printer, or when it is cancelled. Open requests are kept in `load_requests.json`.

## 2.8.0 (unreleased)

- The Filament tab shows and edits the Bambu Cloud filament library (the spool list Bambu Studio and Bambu Handy sync) instead of a local store. The bridge adds `GET|POST /api/filament?account=<id>` and `PUT|DELETE /api/filament/<id>?account=<id>`. `/api/spools` reads and writes through them for every linked account the caller may see; a cloud failure is an error, never old data.
- The price per kg stays in the hub (`spool_prices.json`, by cloud spool id) because the cloud has none. A finished print takes its grams off the matching cloud spool (`netWeight`).
- The hub no longer records AMS trays as "seen" spools; the cloud library gets them from the vendor's own sync. The "Loaded in" selector is gone: the AMS location comes from the cloud and is read-only.
- Migration: `spools.json` is no longer read. Prices of local spools are dropped; spools that exist only in the old file are not copied to the cloud. Enter prices again in the Filament tab.
- Own the actions and the LLM tools of the integration. The integration reads them from `GET /api/integration/manifest`.
- Add `POST /api/actions/<name>` and `POST /api/tools/<name>`.
- Add `manifest_revision` to `GET /api/events`.
- Write camera snapshots and plate images to `/media/druckwerk` from the app. The app maps `media` read-write now.
- Send prints from the app: busy check, G-code guard, bridge call and result per printer. `print_file` is a real action now. The queue and the New print page call it directly.
- Watch prints that run without filament and pause them. The app does it now, not the integration.
- Send the notifications and the `druckwerk_<type>` bus events from the app. The recipients come from `PUT /api/integration/options`. Read the button press from the HA event stream.
- Add `PUT /api/upload/<name>` and `GET /api/files` for the integration.
- Add `GET /api/integration/state` and the `entities` table in the manifest. Add routes for the camera snapshot, the plate picture, the chamber light and the slot re-read.
- Install Pillow in the image to draw a plate picture when the file has none.

## 2.7.0 (2026-11-27)

- Add a 3D G-code preview on the New print page.
- Support orbit, pan and zoom, a layer range, feature toggles and an exploded view for plates with several objects.

## 2.6.0 (2026-11-27)

- Slice in the browser with OrcaSlicer (WASM). The Raspberry Pi is the fallback.

## 2.5.1 (2026-11-27)

- Show status names in the print history.
- Show time left in minutes.
- Hide the empty start card.

## 2.5.0 (2026-11-27)

- Add print options and job names.
- Run AMS drying as a queue job.
- Add media paths and a seen-spools list.
- Show a restart notice.

## 2.4.0 (2026-11-27)

- Add a print history timeline from the Bambu cloud.

## 2.3.0 (2026-11-27)

- Add the Claude feedback button.
- Add a "Create switch" button for grants.

## 2.2.0 (2026-11-27)

- Set up the app and the integration automatically.

## 2.1.0 (2026-11-27)

- Add agent actions and an LLM API: act-as identity, response actions and Druckwerk LLM tools.

## 2.0.0 (2026-11-27)

- First release as Druckwerk. It continues the Bambu Hub app.
