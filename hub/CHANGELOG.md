# Changelog

## 3.34.0 (unreleased)

- Tests: `FakeHA` copies the shared user, device and entity lists, so the suite passes in any file order (a test that added a user broke `test_access_routes.py` when it ran after `test_history.py`).
- Tests: `actions.manifest()` must equal `tests/fixtures/manifest.json`.
- Tests: `.coveragerc` measures the bridge and slicer servers in their subprocesses (`COVERAGE_PROCESS_START`); the bridge suite covers 90 % of `bridge/server.py`. No threshold yet.
- UI mock: printers have `stale` and `status_age` like the real view, and `check_ui.py` checks the stale mark on a card.

## 3.28.0 (unreleased)

- A damaged or empty data file (`queue.json`, `printers.json`, `grants.json`, the sender, notification, price and request files) no longer stops the app at start: it is renamed to `<name>.corrupt-<time>`, one line goes to the log and the app starts with that file empty.
- Data files are written with `fsync` before the rename, so a power loss cannot leave an empty file; a failed write removes its temp file. The saved settings of a queued file and the slicer's `job.json` use the same write.
- A torn saved-settings file no longer gives a 400 on `GET /api/history/<id>/settings` for ever: it counts as not saved and the next source answers.
- A running job no longer rewrites `queue.json` at every tick (360 writes per hour); it is written when the state, error or end time of a job changes.
- Two requests for one cloud file no longer share one `.part` file: the download uses a unique temp file in the target directory and a second request waits for the first.

## 3.27.0 (unreleased)

- An unexpected exception in a route is answered with 500 (`internal error`) and its stack trace goes to the log. Before, the connection was dropped (for example `POST /api/grants {}`).
- Queueing a file by path or a slice: a step that fails after the job was added (copy into the share, plate image, settings) removes the job and its files again. Before, a queued job without a file stayed. The copy still comes after the access check.
- `POST /api/queue`: `filaments` that is not a list of objects and `options` that is not an object are a 400 (they dropped the connection).
- Home Assistant events: the watcher no longer ends when a callback raises, or on any other exception; it logs and reconnects. The socket is no longer left without a timeout: after 60 s without a frame it sends a ping and reconnects when nothing answers. An idle socket stays open.
- Home Assistant websocket commands: a timeout or reset during the login is "Home Assistant is restarting" (503 or the caller's fallback) instead of an uncaught `TimeoutError`; a connect error inside `command` is an `HAError`.
- A client that closes the connection before the answer writes one log line (`client went away`), not a stack trace, in the app and the bridge.
- A failed `turn_on` of a paused automation after a send is logged with the entity id and the error; when the send itself was refused, the job error names the entities that stay off.
- Bridge: an exception while the daemon's output is read ends that daemon and restarts it with the usual backoff, instead of ending the restart loop for good.
- Bridge: `watchdog: http://[HOST]:8098/api/health` in the app config, so the Supervisor restarts the app when the bridge stops answering. Needs `/api/health` to stay open for clients that are not on the loopback (package 1).
- Integration 0.15.1: a manifest with a value it cannot apply is not applied in part. The old actions stay, a warning is logged, the repair issue "bad_manifest" is raised, and the next poll tries again (the revision is kept only after the manifest applied). Actions are tracked as they are registered, so an unload removes all of them. A 401 from the app raises the repair issue "hub_secret_rejected" (before: only "not reachable").

## 3.26.0 (unreleased)

- A print start uses a printer status of at most 60 s. The app reads the bridge's `X-Status-Age` and keeps the real observation time, so a cached status of the bridge no longer looks fresh (before: up to 10 min of the bridge plus the app's own cache). The tolerance of 10 min stays for the display and the ticks. A start with an older status is refused with 409 and the job stays queued.
- One send per printer: a second send to the same printer (action, web UI or queue) gets 409 at once.
- `print_file` also refuses a printer in `PREPARE`. The bridge gives up on its daemon after 280 s (the app waits 300 s) and answers 504 when the daemon timed out, 503 only when it was not reachable at all.
- A queue start whose send ends in a timeout or "not reachable" fails the job (`send timed out; check the printer before you retry`) instead of leaving it queued, where a second start printed twice. A refusal (guard, model mismatch, bridge error) still keeps the job first in the queue.
- The "Plate clear, start" button names the job (`DRUCKWERK_START_<serial>_<job id>`). It starts that job only when it is still the first queued job, the notification is at most 30 minutes old and the phone's Home Assistant user is an admin of the printer (event context user). The app keeps the sent notifications in memory, so a button from before an app restart is refused; use Start in the queue then.
- The actions `start_next` and `start_job` confirm a drying job, so they can start one.
- `POST /api/queue/<id>/cancel` takes `{force: true}` for admins: a `printing` job that never ends is removed as `failed` (`removed by admin`).
- Approving an `any` job needs an admin of a printer that the job fits for its owner, not any printer of the model.
- Printing a browser slice (`POST /api/jobs/<id>/print`) copies it to a file name with a random part, so two prints of the same name no longer overwrite each other. The copy stays while it is the printer's current file (the printer picture reads it) and is deleted when the slice is sent again; a refused send leaves no copy.

## 3.22.0 (unreleased)

- No call to Home Assistant, the bridge or the cloud runs while the queue lock is held. A slow send, spool settlement or cloud login no longer holds up `GET /api/queue`, `GET /api/events` or the bridge status (before: up to the 300 s send timeout).
- New job state `sending` (visible in `GET /api/queue`, shown as "Sending" in the queue): the job holds its printer while the file goes out, so a second start is still refused, then it becomes `starting`, or goes back to `queued` with the error. A job found in `sending` after an app restart is marked failed, because the print may have begun; check the printer before a retry.
- A tick reads the printer status once, before it takes the lock (it read it up to 4 times under the lock).
- Settling a finished job (spools, cost) runs after the queue and sync locks are released; `job_done` follows the settlement as before.
- The "Plate clear, start" notification button and the air-print pause run in their own short-lived thread, so a slow send or pause no longer stops the delivery of Home Assistant events or the ticker.
- Bridge: a cloud login, its code step and the account write hold the global lock only around the account and pending lists; one lock per account keeps two logins of one account in order.

## 3.21.0 (unreleased)

- Bridge (port 8098): only the app on loopback may use it. Other clients get 403, except `GET /api/camera/<serial>/live` (go2rtc cannot send a header) and `GET /api/health`, which no longer lists account email addresses for them. Before, any client in the Home Assistant network could print, pause and delete a cloud account.
- `druckwerk.print_file` obeys the calling user: with a user id only an admin of the printer (HA admin or printer owner) may send, and only they can use `force`. A call without a user stays admin.
- Integration 0.15.0: the to-do list, the chamber light and the buttons send the HA user id (`X-Druckwerk-Act-As`), so the app's rules apply to them. The light and the slot re-read need an admin of the printer (HA admin or owner), as in the web UI. The upload route answers 401 and counts a failed login for a secret with a byte that is not UTF-8 (it was a 500).
- A negative or non-numeric `Content-Length` is a 400 in the bridge and the app (the app read until the client closed). The bridge has a read timeout of 30 s, the app of 120 s.
- The login daemon and ffmpeg of the bridge start with `PATH`, `HOME`, `LANG` and `TZ` only; they no longer get `SUPERVISOR_TOKEN`.
- An empty `HUB_INGRESS_IPS` uses the default ingress address 172.30.32.2. Before, it made each network client a trusted ingress gateway.

## 3.20.0 (unreleased)

- Bridge: the live camera upstream of each printer stays open at all times (`DW_LIVE_KEEP`, on by default). A viewer gets video without the 12 to 14 s mint and keyframe wait on the H2S, and stills and fresh stills (after a light switch) come from the live frames. A dropped upstream opens again after 5 s, then up to 120 s between tries. No new upstream opens while a print is sent. Cost on a Pi 4: about one core and the camera's bit rate per printer, all the time.
- Bridge: a still from the live upstream decodes each keyframe once. Before, each still request ran ffmpeg.

## 3.19.0 (unreleased)

- Nozzle and filament change requests belong to a queue entry, and the entry is blocked until the change has happened. `POST /api/printers/<serial>/nozzle-request` and `POST /api/spools/<id>/load-request` now require `job_id` (a waiting print job; 400 without, 404 for an unknown one; only the job's owner or an admin of the printer may ask). One open request per job and kind. Older load requests without a job stay listed and can be cancelled, they block nothing.
- A job with an open request cannot start: Start, start-next, the Home Assistant services and the "plate clear" notification action refuse with "This job is waiting for 0.6 mm hardened steel nozzle." (or "<spool> to be loaded"). start-next takes the first job that is not blocked. The nozzle mismatch check stays. `GET /api/queue` has `requests` on every waiting job: `[{id, kind, wait, printer, user_id, user_name, note, created_at}]`.
- A request closes by itself when the change happened (nozzle installed; spool in that printer), when it is cancelled, or when its job leaves the queue.
- The owner is notified once per printer and change while an identical request (same spool, or same diameter and type) is still open for another job.
- Queue: a blocked job shows a "Blocked" pill, the reason and who asked, with Start disabled; the row keeps its height. The job page has a "Waiting for a change" card to ask (note optional) and cancel. New print: the nozzle row and the library spool offer "Ask the owner ... when queued"; the request is made for the new job. The Filament tab lists load and nozzle requests with a link to their job; "Ask to load" on a spool is gone.

## 3.18.0 (unreleased)

- Nozzle library. Each printer has a list of the nozzles its owner has (0.2, 0.4, 0.6 or 0.8 mm, with a type such as hardened steel), edited in Setup. The single "Nozzle" choice of the printer form is gone: an old value becomes the first entry of the library at the first start. `PUT /api/setup/printers/{serial}` takes `nozzles: [{diameter, type?}]` instead of `nozzle`.
- The installed nozzle is read live from Home Assistant (`sensor.<prefix>_nozzle_size` and `sensor.<prefix>_nozzle_type` of the vendor integration) and never stored. `GET /api/printers` has `nozzle` (the installed one, `null` when unknown) and `nozzles` (the library plus the installed nozzle if it is not in it). The printer card shows the diameter after "Nozzle X° · Bed Y°", the printer page the diameter and type.
- New print has a nozzle row next to the filament: one chip per nozzle of the printer, the installed one picked at first. The row has the same height while it loads and with one or many nozzles. The slice uses that nozzle's machine and process presets (`engine/presets/<model>-<diameter>.json`; 0.4 mm stays `<model>.json`, unchanged). H2S and P1S have 0.2, 0.6 and 0.8 mm presets with good, better and best layer heights. `nozzle_diameters` in the sliced file follows the choice. A carbon or glass fibre filament (`required_nozzle_HRC` above 3) is refused for a stainless steel nozzle (`GET /api/engine/filament?nozzle=&nozzle_type=`).
- A job keeps its nozzle diameter (`nozzle` in the queue job): from the slice, else from the `; nozzle_diameter` line of the file's G-code (uploads, files under /media or /share, the plates inbox). Starting a job whose nozzle differs from the installed one is refused: "This job needs a 0.6 mm nozzle; the printer has 0.4 mm." An unknown installed nozzle never blocks. The queue shows a "needs 0.6 nozzle" pill and disables Start.
- Ask the owner to change the nozzle: `POST /api/printers/<serial>/nozzle-request`, `GET /api/nozzle-requests`, `DELETE /api/nozzle-requests/<id>`. New print offers "Ask owner to change nozzle" when a nozzle that is not installed is picked, then shows "change requested". One open request per printer, diameter and type. It closes when the installed nozzle matches. The owner gets a notification and the bus event `druckwerk_nozzle_requested`; the Filament tab lists open requests.
- Gap: the direct print `POST /api/jobs/<id>/print` does not check the nozzle (the web UI does not use it).

## 3.17.0 (unreleased)

- The chamber light switch answers at once. It shows the new state on click and sends the command behind it, like the vendor app. Before, the switch was disabled until the command came back (0.7 to 0.9 s on the H2S through Home Assistant, more through ingress), and a poll that drew the card again in that time showed the old state until the next poll (up to 5 s more).
- The new state stays on screen until the printer reports it, or 30 s pass. Before, it stayed 10 s, then the switch could show the old state again until the printer's report arrived.
- A refused command puts the reported state back and shows the error. Commands to one printer go out in click order.
- On the printer page, the switch on the camera now follows the printer's state too. Before, it was drawn once with the camera and kept the state from that moment.
- After a light switch, the camera picture shows the change sooner. The page asks for a picture taken after the command (`fresh=1` on the camera snapshot), and the bridge skips its 30 s snapshot cache for it. Before, a printer card could show the old light for 30 to 40 s. While a live stream is open, a fresh snapshot is the newest frame (the bridge decodes from the last keyframe on). Before, it was the last keyframe, whatever its age. Without a live stream, a fresh picture takes one camera mint plus a keyframe (about 3 to 10 s on the H2S).

## 3.16.0 (unreleased)

- Fix: the printer page said "Printer offline" when the printer was fine. "Offline" now needs evidence: the bridge's cloud flag must say offline in every reading for 60 s (one reading changes nothing), or the status itself says offline, or the last status is older than 10 min. Otherwise the page keeps the last known status.
- The last good status of each printer is kept in `/data/status_cache.json` (written at most every 30 s) and read at start, so a restart of the app or of the bridge no longer blanks the page. It shows the old status as stale, with its real age.
- `GET /api/printers`: `online` is `null` with `status: "connecting"` while no status was ever seen and nothing says offline (first start). The page shows "Connecting" and "Connecting to the printer…". Clients must treat `null` as unknown, not as offline.
- Fix: a printer page that showed "Printer offline" kept showing it after the printer came back, when the camera URL had not changed.

## 3.15.0 (unreleased)

- Bridge log: the first-frame line of a live stream also says when the daemon answered, when the first bytes and the first MPEG-TS came, and which H.264 NAL unit types came before the first keyframe. On the H2S the first frame comes about 10 s after the play loop starts, while a snapshot gets its keyframe in about 1.5 s; this line shows where the time goes.

## 3.14.0 (unreleased)

- Pages show cached data at once after a reload. The identity (`api/me`) and the Access lists (grants, Home Assistant users and entities) are cached in the browser too, so the shell and the lists no longer wait for the network. The cached shape (printer cards, filament slots, list rows) stays on screen, so the page does not jump when the answer arrives.
- While a cached list is still being checked, buttons in the view are dimmed and cannot be clicked (links still work), so nobody decides on old data. The filament view shows cached data as is. Queue and History show a small "Checking for newer items…" pill over the top of the list. It takes no layout space.
- The last camera frame of each printer is kept in the browser (640 px wide) and shows at once with an "n s ago" label until a live frame loads.
- A thin loading bar at the top of the page appears when a request takes 3 s or more.
- Without usable browser storage the UI behaves as before.

## 3.12.0 (unreleased)

- New print has a plate. Step 1 takes several STL files at once (file picker or drop) and puts them on one plate that is sliced as one print. Each model is turned to lie well (auto-orient, on by default, switch it off with "Auto-orient new models") and the plate is arranged with 6 mm between models. A 3D preview shows the bed outline (from the printer's machine preset, 256 x 256 mm until a printer is chosen, and its keep-out area) and every model. Drag to turn the view, wheel or pinch to zoom. The buttons "Auto-orient", "Original orientation", "Arrange" and "Select all" change the plate. A model that is bigger than the bed, or has no room left, is marked "Does not fit the bed" and blocks slicing. A new printer model arranges the plate again on its bed.
- Auto-orient follows OrcaSlicer's Orient.cpp in short: the big face directions (and the six axes) are tried as "down", and the one with the least overhang, the lowest height and the largest contact wins. A cube tilted by 30 or 40 degrees comes back flat, a pyramid or cone stands on its base, a mushroom goes cap down, and a model that already lies well stays as it is. It takes about 0.2 s for 300,000 triangles.
- Object settings now work on a selection, as in Bambu Studio. Click a model in the preview or the list (Shift, Ctrl or Cmd for several). The settings card names the selection, shows the values of the first selected model and writes each change to every selected model. A setting where they differ is marked "varies". "Reset all" resets the selected models. Paint works on one selected model. The list shows how many settings each model has changed.
- You choose the variants to slice: Good, Better and Best are chips in the slice card (at least one stays on, all three at first). The choice is remembered in this browser (`dw-variants`). The button says what it will do ("Slice Better", "Slice Good and Best"), and Compare shows only the sliced ones.
- The engine's error for two models whose paths cross (-101) is shown in plain words on the variant.
- The server still stores one model file per job. For history, queue and Reslice the page uploads the whole plate as one binary STL named after the first model ("a.stl" or "a + 2 more.stl").
- Nothing in the New print layout moves while models, presets or the engine load: the 3D preview and the rows have fixed sizes and placeholders.
- Gap: Reslice brings the plate back as one merged model, without the separate settings and paint of each model.
- Gap: no scale, and no moving or turning of a model by hand. Only auto-orient and arrange.
- Gap: one STL with several bodies is not split into separate models.

## 3.10.0 (unreleased)

- Fix: the top bar keeps one row at every width (the name and then the tab labels give way on narrow windows) and the page no longer scrolls sideways or jumps in height while the window is resized.
- Fix: empty AMS slots on a printer card line up with loaded ones.
- The printer page shows the running print in 3D ("Current print in 3D"). It is the same preview as on a History print, read from that print's stored file. It loads when the cloud lists the print, up to a minute after the start. A user who is not admin of the printer sees it only for their own prints, as in History.
- The printer page has a History card with the last 5 prints of this printer and a link "All prints on this printer".
- History reads a filter from the URL: `#/history?printer=<serial>` (and `user=` for admins). The admin selects start from it. A user sees "One printer" and a link to all their prints.
- This version includes the History work from the history-settings branch (PR 22) (settings, duration bars, download, 3D preview).

## 3.9.0 (unreleased)

- Fix: New print failed with "HTTP 413" for a model or a G-code file above about 1 MB. A proxy between the browser and the app (Home Assistant's ingress) refuses bigger request bodies, and the app's own limit is 100 MB. The page now sends models and G-code in parts of 512 KB and the app joins them (`total` and `offset` in the query of `POST /api/jobs`, `POST /api/jobs/{id}/model` (new) and `POST /api/jobs/{id}/gcode`, see API.md). A request without `total` works as before.
- New print has a step "Object settings" with the per-object settings of Bambu Studio and OrcaSlicer. Always visible: seam (position, gap, scarf joint), supports (type, style, threshold angle, on build plate only, distances), fuzzy skin ("surface randomisation": mode, thickness, point distance, generator, noise), walls and shells, infill, layer height, ironing and brim. The list "Add a setting" has every other object setting of the engine (about 300). A changed value replaces the preset value in all three slices. The form, the greyed rows, the warnings and the values that an edit changes along with it come from the engine's own settings service. The arrow returns a value to the preset.
- Paint supports and seam on the model: support enforcers and blockers, seam enforcers and blockers. A brush and a "fill by angle" tool work on a refined copy of the mesh (same shape). Painted support turns supports on by itself. The slice button is now its own card below the settings.
- Gap: fuzzy skin painting is not offered. With this engine (v0.6.0-m1.9) fuzzy skin "painted only" gives the same G-code as no fuzzy skin. The fuzzy skin setting itself works.
- Gap: the model cannot be rotated or scaled in New print yet. Painted meshes are sent as they are (no repair), so a broken STL can fail with painting where it slices without.

## 3.8.0 (unreleased)

- The printer page plays the live camera stream. H.264 printers (H2S, X1) send MPEG-TS, which the vendored mpegts.js 1.8.0 (Apache-2.0) plays through Media Source, or ManagedMediaSource on iOS 17.1+. JPEG printers (P1, A1) show the multipart stream in an `<img>`. Before, the page showed a still that the bridge cached for 30 s.
- The still stays under the video as poster and as fallback. A stream that fails, ends or freezes for 10 s starts again (backoff 1 s to 30 s). A hidden page drops its stream. Printer cards on the grid keep the stills.
- Bridge: an unused live upstream stays open 90 s (was 30 s, `DW_LIVE_IDLE`), so a viewer who comes back gets a frame at once. A live upstream costs about one Pi 4 core while open (qemu runs the vendor plugin).
- Bridge: when the camera URL mint times out on a live open, the bridge mints once more before it answers 502. Mints fail about 1 in 7 on the H2S.
- Bridge log: `live <serial>: first frame after N s` and, at close, the duration, MB, stalls (gaps over 2 s, `DW_LIVE_STALL`) and the longest gap.
- `GET /api/printers` has `camera_live`: `mpegts` or `mjpeg`.

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

- History detail shows all slicing settings of a print: Quality, Strength, Support and adhesion, Speed, Filament, Plate and bed, Printer, and an "All settings" list with every key and a search field. They are read from the sliced file (`Metadata/project_settings.config` and `slice_info.config`) by `hub/slice_settings.py`. `GET /api/history/{id}/settings` is new. The settings are saved when a job is queued, so they stay after the slicer forgot the slice.
- One primary action on a history item: "New print job with the same settings". It queues the stored file again with the print's target and options (`POST /api/history/{id}/queue` is new). When the file is gone it opens New print with the same printer, options, quality, filament and plate; choose the model again. "Print again" is replaced by this button.
- Prints that were not started in Druckwerk: the bridge now passes `model_id`, `profile_id` and `design_id` of the Bambu cloud task, and `GET /api/tasks/file` asks the cloud for the file URL. The hub fetches the 3MF once into `data/cloud` and reads the same settings. If the cloud gives no file, the detail says so and shows the fields of the cloud record. The cloud file request is not checked against a real account yet.

- History shows how long each print took: under every entry a bar sits on the day's 00:00 to 24:00 axis, as wide as the print ran (start to end; a running print reaches to now). Colour is the status. A print past midnight is cut at 24:00 and marked, very short prints get a minimum width, and prints without times show no bar.
- History detail has a download button for the print's model (the STL, 3MF or STEP the slicer still stores), else for the sliced 3MF (Druckwerk's, or the Bambu cloud's). With no stored file the button is not shown. `GET /api/history/{id}/file` is new and follows the same visibility as the history.
- History detail shows the 3D preview of the print, read from the same stored file as the download (the slicer's sliced file, a file in the share, or the Bambu cloud's 3MF), so prints from the cloud have it too. It loads only when the details open, shows a placeholder while loading and when no file is stored. `GET /api/history/{id}/gcode` is new; `GET /api/history/{id}/settings` now also says `download` and `preview`.

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
