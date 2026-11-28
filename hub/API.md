# Druckwerk API (ingress port)

Contract between the app backend (`hub/`), the web UI (`hub/ui/`) and the `druckwerk` integration.
Paths are relative to the ingress root. All bodies are JSON. Errors: `{"error": "<text>"}` with 400, 401, 403 (text starts `rule:`), 404, 409, 502.

Identity comes from the Supervisor ingress headers `X-Remote-User-Id`, `X-Remote-User-Name`, `X-Remote-User-Display-Name`.
Calls from the integration go to `http://<app hostname>:8099` without ingress and carry `X-Hub-Secret`; they are internal (admin everywhere).
An internal call may add `X-Druckwerk-Act-As: <HA user id>`. The app then applies that user's rules exactly as for an ingress user (HA admin flag from the HA users list, grants, ownership of slices and jobs), so a non-admin cannot skip, approve, start or cancel other people's jobs. The user must be an active, non-system HA user, else 403 `rule: act-as user <id> is not an active Home Assistant user`. Without the header the internal caller stays admin (automations). The header counts only together with a valid `X-Hub-Secret`; on an ingress request it is ignored. An act-as caller is not internal, so internal-only routes (`/api/events`) answer 403 for it; HA-admin routes work for act-as HA admins. The integration sends the header with `call.context.user_id` for its agent actions and LLM tools.
The app only trusts the identity headers from the Supervisor ingress gateway (172.30.32.2, env `HUB_INGRESS_IPS`); every route except the web UI's own files (`GET /`, `/index.html`, `/style.css`, `/app.js`, `/api.js`, `/util.js`, `/views/*.js`, served from hub/ui) answers 401 without an identity or the secret.
The secret reaches the integration through Supervisor discovery: on start the app POSTs `{service: "druckwerk", config: {host: <own hostname>, bridge_port: 8098, hub_port: 8099, secret}}` to `http://supervisor/discovery` (retried every 30 s until it works). `GET /api/integration` shows the same data to HA admins as the fallback.

## Me

- `GET /api/me` → `{user_id, name, is_admin, setup_done, printers: [{serial, role: "admin"|"user"}]}`

## Setup (HA admins only)

- `GET /api/setup` → `{accounts: [{id, email, logged_in, daemon_running}], printers: [{serial, device_name, model, account, configured, suggested_prefix}]}`
  `suggested_prefix` is the prefix saved for the printer, or the one a registration would use: the vendor integration's entity prefix for that serial when HA has its device (the entity registry's `<serial>_print_status` entity minus `sensor.` and `_print_status`, else the slug of the device name), else the slug of the printer's name (with `_<serial>` added when another printer has that prefix). Without the vendor integration it is only a name: no HA entity has to exist.
  `config` is added to each printer: `null`, or the saved printer (shape of the `PUT` answer) for editing.
- `POST /api/setup/login/start` `{email, password}` → `{state: "done"|"need_code"|"need_tfa", account}`
- `POST /api/setup/login/code` `{email, code}` → same
- `DELETE /api/setup/accounts/{id}`
- `PUT /api/setup/printers/{serial}` `{name, ha_prefix, plate, nozzle, pause_entities: [entity_id], electricity_kwh_price?: number, owner?: HA user id}` → `{serial, name, model, account, ha_prefix, plate, nozzle, pause_entities, electricity_kwh_price, owner}`. `owner` is the printer's owner (an active HA user id, `""` for none; 400 for an unknown user; left out = unchanged). The owner is an admin of that printer (queue, spools, start), gets load requests and approval notifications, and is named in the history for prints nobody started here. Ownership is a printer property, not a grant: at start every `owner` grant is moved into the printer's `owner` (a printer that has an owner keeps it; the grant then stays as a `user` grant; `printer: "*"` applies to every printer without an owner) and the grant is dropped once its user owns every printer it covered.
  `model` and `account` come from the bridge; `name` is required (400). `ha_prefix` is optional and defaults like `suggested_prefix`; HA does not need to have any entity with it. 404 if no bridge account has the serial.
- `DELETE /api/setup/printers/{serial}`
- `GET /api/integration` → `{url, secret, connected}` (HA admins; fallback for the config flow, same data as the discovery message). `connected` is true when HA has a `druckwerk` config entry (read over the HA websocket, false on error).
- Printers register themselves: every printer the bridge lists (the bound printers of the logged-in accounts) is saved with default settings. HA and the vendor integration are not involved. It runs at start, on `GET /api/setup` and every 60 s. A printer removed with `DELETE /api/setup/printers/{serial}` is not registered again until a later `PUT` for it.
- `/api/bridge/*` proxies the bridge's own page and API (HA admins only).

## Printers (filtered to printers the caller has an active grant on; HA admins see all)

- `GET /api/printers` → `[{serial, name, model, role, online, status, stage, progress, remaining_min, end_time, layer, total_layers,
  nozzle_temp, nozzle_target, bed_temp, bed_target, chamber_temp, task_name, cover_url, camera_url, account, light: true|false|null,
  active_tray: {unit, slot} | null,
  ams: [{unit, humidity_index, temp, trays: [{slot, type, colour: "RRGGBB", name, remain_pct, spool_id}]}],
  external: {type, colour, name} | null,
  hms: [{code, severity: "fatal"|"serious"|"common"|"info", text, url}],
  drying: [{ams_id, name, status, active, left_min, filament, temp, hours, temp_min, temp_max}],
  current_job: job | null, queue_length}]`
  Values come from the bridge's MQTT status of the printer (`GET /api/status/<serial>`, mapped in `hub/printer_state.py`), on every install. `online` is `true` while a status exists that is not `offline` and is at most 10 min old (older than 60 s: `stale`), `false` when the cloud flag has said offline for 60 s, the status says offline or the last status is older than 10 min (values are then null or empty), and `null` with `status: "connecting"` when no status was ever seen and nothing says offline. The last status is kept in `/data/status_cache.json` across restarts. `status` is the lower-case `gcode_state` (`idle`, `prepare`, `running`, `pause`, `finish`, `failed`, ...), `stage` the printer's stage name (`printing`, `idle`, `changing_filament`, `paused_filament_runout`, ...; null for an unlisted id). `progress` is a number 0..100, `remaining_min` whole minutes, `end_time` an ISO time (now plus the remaining time, only while preparing, running or paused, else null), temps whole degrees; unavailable values are null.
  `active_tray` and tray `slot` are 0-based, `unit` is the AMS number (0 = "AMS 1"; an external spool is unit 255, slot 0); AMS units, trays and humidity come from the `ams` object of the status (rack units are left out). A tray's `name` is its sub-brand, else its type. `humidity_index` is 1 (dry) .. 5 (wet), the reverse of the printer's own index. `hms` has an entry per HMS error: `code` is `HMS_AAAA_BBBB_CCCC_DDDD`, `severity` comes from the code, `url` is the vendor's HMS page, and `text` is always empty (no error texts are shipped). Empty slots have `type: ""`. `spool_id` is the spool assigned to that tray, or null. `camera_url` and `cover_url` are ingress-relative.
- `drying` lists the AMS units that have a dryer (AMS 2 Pro, AMS HT) and whose printer supports remote drying (bridge `GET /api/drying/{serial}`, cached 5 s); `[]` when the bridge cannot say. `ams_id` is the AMS number as the printer uses it (0 to 3, 128 and up for an AMS HT), `status` is `off|checking|drying|cooling|stopping|error|testing`, `active` is true while the AMS is drying, `left_min` the minutes left.
- `light` is the chamber light as the printer reports it (`lights_report`, node `chamber_light`), null when it reports none. `account` is the Bambu Cloud account the printer belongs to (the `account` of its spools).
- `POST /api/printers/{serial}/light` `{on: true|false}` (admin of the printer or HA admin; 403 otherwise, 400 for a body without a boolean `on`) → `{on}`. Sent through the bridge (`POST /api/light/<serial>`), like the integration's light entity; the printer reports the new state at the next poll.
- `start` and `start_job` in the printer list: while a print starts, `start` is `[{id, state, percent, detail}]` for `upload`, `download`, `prepare`, `print` (`state` is `done`, `active` or `pending`; `percent` and `detail` may be null; `detail` of `prepare` is the printer's stage name) and `start_job` the queue job being started; both are null otherwise. Upload comes from the bridge (`GET /api/print-progress/<serial>`), download and prepare from the printer status (`gcode_file_prepare_percent`, stage).
- `GET /api/printers/{serial}/camera` → MJPEG stream of the cloud camera (bridge `/api/camera/{serial}/live`, query string passed on); with `?mode=snapshot` one JPEG (`/snapshot`) with the header `X-Snapshot-Age` (seconds since the bridge took the picture; a picture younger than 30 s is sent again without a new camera call). The web UI only uses snapshots, because H.264 printers stream MPEG-TS, which an `<img>` cannot show. Needs any active grant on the printer.
- `GET /api/printers/{serial}/cover` → the current print's picture: the plate image of the file Druckwerk sent (or the file named like the printer's task), else the cover of the newest cloud task with the printer's task name (fetched by the app), else 404. Needs any active grant. `cover_url` in the printer list is set while the printer reports a task name.
- `POST /api/printers/{serial}/start-next` `{plate_clear: true, confirmed?: true}`; `POST /api/printers/{serial}/start` `{job_id, plate_clear: true, confirmed?: true}` (admin of printer). A print job needs `plate_clear: true`, a drying job needs `confirmed: true` (409 otherwise).
- `POST /api/printers/{serial}/drying` `{ams_id, temp, hours, filament?, confirmed: true}` (admin of the printer) → the running drying job. Dries now: it creates a drying job in the queue and starts it, so the busy checks are the same (409 when the printer is not idle, finished or failed, or has a job running). 400 for values the bridge refuses (temperature outside the dryer's range, hours outside 1 to 72, a unit without a dryer); a refused start leaves no job.
- `POST /api/printers/{serial}/drying/stop` `{ams_id}` (admin of the printer). Stops the AMS and ends the running drying job of that unit; drying that was started elsewhere is stopped too.

## Queue (see hub/queue/queue_routes.py and queue_access.py for rules)

- `GET /api/queue` → `{jobs, history, printers}`; job = `{id, kind: "print"|"dry", name, file, source, target, model, filaments, options, state, error, created_at,
  owner_user_id, owner_name, printer?, started_at?, finished_at?, grams?, cost?, plate_png?}`
  states: `pending_approval, queued, starting, printing, drying, done, failed, cancelled`.
  `name` is the print's name (at most 80 characters; default: the file name without `.gcode.3mf`). The integration's todo item and notifications show it, and the `job_*` events carry it in `job`.
  `options` (print jobs): `timelapse` (false), `use_ams` (true), `vibration_cali` (true), `layer_inspect` (false), `ext_change_assist` (false) as booleans; `bed_leveling` (true), `flow_cali` (true), `nozzle_offset_cali` (false) as `true|false|"auto"`; `bed_type` (`auto|cool_plate|eng_plate|hot_plate|textured_plate|supertack_plate|3d_effect_plate`). The values in brackets are the defaults, equal to what the app sent before the options existed; anything else is a 400 `bad option`. At the start the app passes the advanced options on only when they differ from the default.
  A drying job has `kind: "dry"`, `file: ""` and `dry: {ams_id, temp, hours, filament}`; it targets one printer (never `any`).
- `POST /api/queue` `{file, source?, target, model?, filaments?, options?, name?}`. `file` is a `.gcode.3mf` name in the share, or an absolute path under `/media` or `/share` to a sliced one: the path is resolved (symlinks too), must stay inside `/media` or `/share`, must be a 3MF with `Metadata/plate_N.gcode`, and is copied into the share under a unique name; grams, print time, filaments and the plate image are taken from it (400 otherwise). `{kind: "dry", target: serial, ams_id, temp, hours, filament?, name?}` adds a drying job: `ams_id` 0 to 3 or 128 to 135, `temp` 45 to 85 (the bridge checks the unit's own range), `hours` 1 to 72. It follows the same grant rules and approval flow as a print.
- `POST /api/queue/{id}/edit` `{name?, options?}` changes the name (at most 80 characters) or print options (same values as `options` above; unnamed options keep their value) of a job that is still `queued` or `pending_approval` (owner of the job or admin of its printer; 404 once it left the queue, 400 for a drying job or a bad value). A job's target is changed with `move`.
- `POST /api/queue/{id}/{reorder|move|cancel|retry|approve|reject|skip|stop}`. `stop` ends a running drying job and stops the AMS (admin of the printer; 409 if the job is not drying; the job stays when the bridge cannot stop it). `cancel` is for waiting jobs. A drying job cannot move to another printer. Drying jobs take part in the order of the printer's queue: the next job for a printer is the first waiting job, a print or a drying. While a drying runs (`drying`), no job starts on that printer. The job ends as `done` when the AMS stops reporting a drying (checked every 10 s, not in the first 2 minutes) or, when the state is unknown, 10 minutes after the set time. Drying jobs raise `approval_needed` but no `job_started|done|failed` events and use no spools.
  `reorder` takes `{position, scope?: serial}`: with `scope`, `position` is 0-based among the waiting (queued + pending_approval) jobs whose target or printer is that serial (other jobs keep their slots); without it, global. Access rules are unchanged: a user's reorder only permutes their own slots.
- `GET /api/queue/{id}/plate.png` → the job's plate image (kept with the job after the slice is pruned); visible to whoever sees the job.
- A job made from a slice (`POST /api/jobs/{id}/queue`) also carries `slice_job` (the slicer job id; its model and G-code exist only while the slicer keeps that job) and `slice`: `{quality, process, filament, bed, tray: {ams, id, ...}, seconds, grams, filaments: [{type, colour, grams}], model}`, kept with the job so the detail view can show how it was sliced.
- The job also carries `filament_grams` (per filament), `print_seconds` (slice estimate), `spools_used` (`[{spool_id, grams}]`, after done).

## History (hub/history.py)

- `GET /api/history?user=&printer=&limit=` → `{items, unavailable}`. The Bambu cloud's task list is the source of truth (per printer, via the bridge's `GET /api/tasks?serial=&limit=<=100`, cached 60 s). Druckwerk jobs only say who started a task: same printer, same file or title name (extension and case ignored) and the task started 60 s before to 15 min after the job; the closest start wins, each task and job once.
- item = `{id, serial, printer_name, title, status, status_code, started_at, ended_at, seconds, grams, cover_url, plate, design_title, job_id, user_id, user_name, cost, source, job, when, printer_owner?}`; times are epoch seconds. `source` is `druckwerk` (a job matched; `job` is that job: `id, name, state, error, file, source, target, model, filaments, options, spools_used, grams, filament_grams, print_seconds, cost, plate_png, slice_job, slice, owner_*, created_at, started_at, finished_at`, absent fields left out), `cloud` (started at the slicer or printer; `user_id`, `user_name`, `job_id`, `cost`, `job` are null; `printer_owner` is the display name of the printer's owner or null) or `job` (a finished Druckwerk job (`done`, `failed`, `cancelled`) that no cloud task explains, for example one that never reached a printer: `id` is `job-<job id>`, `status` is `finished|failed|cancelled`, `cover_url` null, `error` the job's error). `when` is `started_at`, else the end or creation time, and orders the list; it is never null for `job` items. `status` is `finished|failed|cancelled|running`, or the raw cloud value when it is not known; `status_code` is always the raw value.
- `GET /api/history/{id}/settings` → `{available, source, reason, can_reprint, task, slicer, groups, all, plate}` for one history item the caller may see (404 otherwise). `groups` is `[{title, rows: [{key, label, value, unit}]}]` (Quality, Strength, Support and adhesion, Speed, Filament, Plate and bed, Printer; empty groups are left out; per-filament values are those of the filaments the plate uses). `all` is `[[key, text]]` for every key of `project_settings.config` (long values cut at 300 characters). `plate` is the metadata of `slice_info.config` (print time `prediction`, `weight`, `objects`, ...). `source` is `slice` (the slicer still has the file), `saved` (kept when the job was queued, in `data/settings`), `cloud` (the 3MF of the Bambu cloud task, fetched once into `data/cloud/<task id>.gcode.3mf`) or null. When null, `available` is false, `reason` says why and `task` holds the cloud record's fields (`title, design_title, status_code, plate, seconds, grams, started_at, ended_at, model_id, profile_id, design_id`, those that exist). `can_reprint` is true when a sliced file exists for `POST .../queue`. A failed cloud fetch is not tried again for 10 minutes.
- `POST /api/history/{id}/queue` `{target?, model?, options?, name?}` → queue job. Queues the stored sliced file again: the slicer's file if it still has it (same result as `POST /api/jobs/{id}/queue`, owner or global admin), else the cloud 3MF (fetched if needed). Defaults: the print's printer, options and name. Normal grant rules apply (403, or `pending_approval`). 409 `No stored file for this print: ...` when there is no sliced file; 404 for an unknown or hidden item.
- Newest first. HA admins and printer owners see all items of their printers and may filter by `user` (an HA user id or `unknown`) and `printer`. Other users see only items attributed to themselves; `user` is ignored for them. `unavailable` lists serials whose cloud answer failed (items of the others still come).
- `GET /api/spools/{id}/prints` → `{items}`: the history items (same shape and visibility as `/api/history`) whose Druckwerk job used that spool (`job.spools_used`), newest first, each with `spool_grams`.
- Bridge: `GET /api/tasks?serial=<serial>&limit=<1..100, default 50>` uses the token of the account that owns the printer; 404 unknown printer, 502 when the cloud fails or refuses the token. Tasks also carry `model_id`, `profile_id` and `design_id` (cloud `modelId`, `profileId`, `designId`) when the cloud record has them.
- Bridge: `GET /api/tasks/file?serial=&model_id=&profile_id=` → `{url}`, the download URL of the task's sliced 3MF (cloud `GET /v1/iot-service/api/user/profile/{profile_id}?model_id=`; `url`, or the first `profile_print_3mf[].url`, must be https). 400 for bad ids, 404 unknown printer, 502 when the cloud fails or sends no URL. Not checked against a live account.

## Slicing

- The app does not slice. The browser slices with the WASM engine and uploads the G-code (below). Since 3.3.0 `POST /api/jobs/{id}/slice` does not exist (404), and neither do the actions `slice` and `get_slice` and the tools `druckwerk_slice` and `druckwerk_get_slice_results`.
- Job routes: `POST /api/jobs?name=` (upload a model; `.stl`, `.3mf`, `.obj`, `.step`, `.stp`; state `uploaded`), `GET /api/jobs/{id}` (a job with stored browser G-code also carries `cost_estimate`: filament from the matching spool plus electricity, same formula as the final job cost; `null` when it cannot be read), `GET /api/jobs/{id}/(plate.png|plate.gcode)`, `POST /api/jobs/{id}/print` (direct print, admin of the printer only; the queue is the normal path).
  A job belongs to the user who uploaded it (`owner_user_id`); others get 403, HA admins and the integration may use any. Storing G-code needs an active grant on the chosen printer. After an update from a version that sliced on the server, a job left in state `queued` or `slicing` is `failed` ("This job was left by an older version. Slice again in the browser.").
- Parts: a proxy in front of the app (HA ingress) can refuse request bodies of about 1 MB (HTTP 413 without JSON). The web UI sends models and G-code in parts of 512 KB. `POST /api/jobs?name=&total=<bytes>` with the first part (offset 0) answers the job with `state: "uploading"` and `received`. `POST /api/jobs/{id}/model?offset=&total=` adds the next part (`409 {error, received}` for a wrong offset); the job is `uploaded` when `total` bytes are there. `POST /api/jobs/{id}/gcode` takes `offset` and `total` with the same query fields in every part and answers `{received, total}` until the last part, then the job. A part is at most 1 MiB (`413`). Without `total` the body is the whole file, up to 100 MB, as before.
- `GET /api/jobs/{id}/model` → the uploaded model file (same ownership rule as the other job routes; 404 once the app pruned the job). Reslice uses it to open New print with the same model.
- `POST /api/jobs/{id}/gcode?serial=&quality=&tray=&bed=&filament=&seconds=&grams=&metres=&engine=&slice_s=` (body: G-code): G-code sliced in the browser, packed as a `.gcode.3mf` and stored like a slice (no plate image: `info.has_png` is false). Needs identity, an active grant on the printer (403 `rule: slicing needs an active grant on the printer`) and the job's owner. `GET /api/engine/filament?serial=&tray=` → `{filament}`, the preset the browser slices a tray with (grant needed). Instead of `tray`, a library spool that is not in the AMS: `type` (required), `brand`, `name`, `colour` (RRGGBB), `spool` (cloud id). Its preset is the one whose filament id the tray reports, else one of that type whose name has the brand's first word and every word of `name`, else `Generic <type>`. `/gcode` takes the same fields in its query; the job's `settings.tray` then has `ams` and `id` null, and the queued job carries `slice_spool: {spool_id, brand, type, name, colour}` (null for a tray slice; `slice_tray` is null then). 400 `Pick a loaded AMS tray or a spool from the library.` when neither is given. `GET /engine/...` serves the WASM engine and the flattened presets.
- `POST /api/jobs/{id}/queue` `{target: serial|"any", model?, options?, name?}` → queue job (file, grams and plate image taken from the stored G-code). The web UI uses it; the action `add_to_queue` has no `slice_job_id` any more (nothing hands an agent a job id) and takes only `file`, now required. The 3MF is copied into the share under a unique name; the add follows the normal grant rules (403, or `pending_approval`).

## Filament and cost

- `GET /api/spools` → the Bambu Cloud filament library of every linked account the caller sees (HA admins: all; others: accounts that own a printer they see, without spools loaded in printers they cannot see; 409 when none is linked, 502 `Bambu Cloud filament library: ...` when the cloud fails). `[{id (cloud id, digits), brand, type, name, colour: "RRGGBB", grams_left|null, grams_total|null, price_per_kg|null, printer|null, unit|null, slot|null, in_printer, rfid|null, account}]`. The list is cached for 30 s and dropped after a write. `price_per_kg` is kept by the hub, the cloud has no price.
- `POST /api/spools` (`brand, type, name, colour, grams_left, grams_total, price_per_kg, account`; `name` and `colour` required, `account` only when several are visible), `PUT /api/spools/{id}`, `DELETE /api/spools/{id}` write to the cloud (HA admin, or admin/owner of a printer of the spool's account). `price_per_kg` is stored in the hub and never sent to the cloud. The AMS location cannot be changed.
- `POST /api/spools/{id}/load-request` `{printer, note?}` (note up to 200 characters) asks the printer's owner to put a library spool into the AMS. Any user who sees the printer and the spool may ask; 400 when the printer has no AMS, 409 when the spool is already in that printer. Answers the request `{id, spool_id, spool_label, colour, printer, user_id, user_name, note, created_at}`; an open request for the same spool and printer is returned as it is. Fires the event `load_requested` (`druckwerk_load_requested` on the HA bus, payload `request` with `printer_name`) and notifies the printer's `owner` if they are at home; HA admins who do not own the printer are not notified. Without an owner the request is kept and only logged.
- `GET /api/load-requests` → open requests for printers the caller sees. `DELETE /api/load-requests/{id}` by the requester, an admin or owner of the printer, or an HA admin (403 otherwise). A request also closes when a library read shows the spool in that printer (`printer` equals the request's printer and `in_printer`).
- A job's `grams` comes from the sliced 3MF (`Metadata/slice_info.config` filament used_g per filament). On `done` (exactly once) the grams are taken off the cloud spool (`PUT` of the new `grams_left`) in the tray the slice was made for, else a spool of the same type and colour on that printer; never below 0.
  `cost` = sum(grams × price_per_kg / 1000) + (print hours × printer watt estimate / 1000 × electricity_kwh_price), rounded to cents. `print hours` is the slice's estimate. Watt estimate per model in code (H2S, X1, P1S, P1P, A1, A1 mini). A filament without a matching spool, or whose spool has `price_per_kg: null` ("no price", allowed), costs 0. The queue copy of the 3MF is deleted when the job is done.

## Access

- `GET/POST /api/grants`, `DELETE /api/grants/{id}` (HA admins; GET also internal). Answers carry `user_name` (HA user name) and `active` (until/condition evaluated now). Grant: `{id, user_id, user_name, printer: serial|"*", role: "user", until?, condition_entity?, needs_approval?, active}`.
- `GET /api/ha/users` → `[{id, name, is_admin}]` (HA admins and internal), from HA `config/auth/list`; system and inactive users are left out.
- `GET /api/ha/entities?domains=schedule,input_boolean,binary_sensor,person` → `[{entity_id, name, state}]` (HA admins), for the condition picker.
- `POST /api/ha/switches` `{user_id, printer: serial|"*"}` → `{entity_id, name, state: "off"}` (HA admins, like grants). Creates an `input_boolean` helper through the HA websocket (`input_boolean/create`, icon `mdi:printer-3d`) named `Druckwerk <user name> <printer name or All printers>`. The entity may be missing from `/api/ha/entities` for a moment; a grant accepts it as `condition_entity` anyway. 400 for an unknown user or printer.
- `POST /api/feedback` `{item, description}` → `{ok: true}` (any HA user; the body may be up to 1500000 bytes, other routes allow 8 KB). Calls HA `todo.add_item` on `todo.feedback`; `item` is 1 to 300 characters, `description` a non-empty string (JSON text with the context and a screenshot). 400 for a bad body. Sent by the feedback button (`hub/ui/feedback.js`).

## Home Assistant restarts

While Home Assistant restarts (HTTP 502, 503, 504 or no answer), a route that needs it answers `503 {error: "Home Assistant is restarting. This page tries again in a few seconds.", retry: true}`. The web UI shows that text in a banner and asks `GET /api/me` with a growing pause (2 s up to 20 s) until it works, then draws the view again.

## Events for the integration

- `GET /api/events?since=<seq>` (internal) → `{seq, manifest_revision, events: [{seq, type: "job_done"|"job_failed"|"job_started"|"approval_needed"|"queue_changed", serial, job}]}`.
  Without `since` the answer is `{seq: <current>, manifest_revision, events: []}` (no backlog). `manifest_revision` is the `revision` of `GET /api/integration/manifest`. `job` is the full job object (always with `id, file, owner_name, target, printer`; `printer` is null until a printer took it); `serial` is the job's printer or fixed target, `""` for `queue_changed` (then `job` is null) and for `any` jobs not yet placed.
  `job_started` fires when the print was sent, `job_failed` when a started job failed. Events are kept in memory (last 500); `seq` starts at the boot time in seconds, so it keeps growing across restarts.
  The app fires the HA bus events and the notifications itself since 2.8.0 (see "Generic integration"). An integration that still polls this and fires them too doubles them.

## Integration manifest, actions and tools (thin integration)

Goal: the `druckwerk` integration (HACS, a Home Assistant restart on every update) only forwards. What it offers is described by the app, which HA updates without a restart. All routes here are internal: `X-Hub-Secret` is required, `X-Druckwerk-Act-As` is optional and works as everywhere else (see the top of this file).

- `GET /api/integration/manifest` → `{api_version: 1, revision, actions: [...], entities: [...], llm: {prompt, tools: [...]}}`. `revision` is the first 12 hex digits of the SHA-256 of the rest, so it changes only when the content changes. The integration polls it with the events poll (`GET /api/events` also returns `manifest_revision`) and re-registers when it differs. `api_version` is an integer; the integration states the highest it speaks and raises a repair issue when the app's is higher.
- The revision hashes `json.dumps(rest, sort_keys=True, separators=(",", ":"))`.
- An action = `{name, title, description, response: "none"|"optional"|"only", serial_fields: [field names], fields: {<field>: {required: bool, default?: any, title, description, example?, selector?: <HA selector object>, advanced?: true}}}`. `title` is the name the HA UI shows, `advanced` marks a field HA hides behind "show more options". Names, field names, requiredness, defaults and `response` of the 14 current actions (`print_file`, `get_printers`, `get_queue`, `add_to_queue`, `cancel_job`, `reorder_job`, `move_job`, `skip_job`, `approve_job`, `reject_job`, `retry_job`, `start_next`, `start_job`, `camera_snapshot`) are a contract: `custom_components/druckwerk` has a golden test (`tests/golden/contract.json`) for them. `serial_fields` lists fields whose value may be an HA device id; the integration replaces such a value with the printer serial before it forwards the call (the app cannot read the HA device registry).
- `print_file` is a real action since 2.8.0: the app sends the file itself (see "Generic integration").
- `POST /api/actions/<name>` with the action's data as JSON (the app validates and coerces: booleans, `"auto"` for tri-state options, ranges) → `200 {result: <object>}` (`{}` when the action has no response). `400 {error}` is a validation error, `403 {error: "rule: ..."}` an access rule, `404` an unknown action or job (`slice` and `get_slice` are gone since 3.3.0: the app does not slice), other errors as elsewhere. A validation error text follows the format of Home Assistant's schema errors, for example `required key not provided @ data['job_id']`. The act-as user applies exactly as for the web UI. `camera_snapshot` writes its image in `/media/druckwerk/` itself (`map` has `media:rw`).
- `llm.tools` = `[{name, description, parameters: <JSON schema, type object>, annotations: {read_only, destructive, idempotent, open_world}}]`, the 13 tools `druckwerk_<action>` of the LLM API "druckwerk" (names are a contract, see the golden file). `POST /api/tools/<name>` with the tool arguments → `200 {result: <object>}`; errors as for actions. `llm.prompt` is the API prompt text.
- Anything the integration needs from HA stays in the integration: the device id lookup (`serial_fields`) and the HA user id.

## Generic integration: entities, print, files, upload, options

Goal: the `druckwerk` integration holds no logic of its own. It renders what the app describes, and forwards. All routes here are internal: `X-Hub-Secret` is required (401 without identity or secret, 403 `rule: only the Home Assistant integration can do this` with an identity but no secret). `X-Druckwerk-Act-As` is ignored on them, except on `POST /api/actions/*` and `POST /api/tools/*`, where it works as above. Errors are `{error: <text>}`. The integration maps 400 to `ServiceValidationError` and every other status to `HomeAssistantError`, both with the `error` text, and a connection failure to `HomeAssistantError("Druckwerk not reachable: ...")`.

Two base URLs: `hub_url` is the app on port 8099 (every route below). `bridge_url` is the bridge on port 8098, same host. Only the camera stream (see Camera) uses the bridge URL.

### Routes

| Route | What it does |
| --- | --- |
| `GET /api/integration/manifest` | Actions, LLM tools and the entity table. |
| `GET /api/integration/state` | What every entity shows, per printer. |
| `PUT /api/integration/options` | The integration pushes its options. |
| `POST /api/actions/print_file` | Send a file to a printer. |
| `GET /api/files` | The print files. |
| `PUT /api/upload/<name>` | Store an uploaded print file. |
| `GET /api/integration/printers/<serial>/image` | The picture of the current print (PNG). |
| `GET /api/integration/printers/<serial>/snapshot` | The camera still (JPEG). |
| `POST /api/integration/printers/<serial>/light` | Switch the chamber light. |
| `POST /api/integration/printers/<serial>/rfid` | Make the AMS read a slot's tag again. |
| `POST /api/queue/<id>/cancel`, `POST /api/queue/<id>/reorder` | The existing queue routes, used by the todo entity. |

`<serial>` matches `[A-Za-z0-9_-]{1,40}`.

### Manifest: `entities`

The manifest has a new key `entities`, an ordered list. It is part of `revision`, so a change reaches the integration through `manifest_revision` as before. `api_version` stays 1: the change only adds a key.

```json
{"key": "start_print", "platform": "button", "name": "Start cloud print",
 "press": {"path": "/api/actions/print_file",
           "body": {"serial": "$serial", "file": "$print_file", "timelapse": "$timelapse",
                    "bed_leveling": "$bed_leveling", "use_ams": true},
           "needs": {"print_file": "Select a print file first"}}}
```

One entry makes one entity per printer.

| Field | Type | Meaning |
| --- | --- | --- |
| `key` | string | The unique id is `<serial>_<key>`. These ids must not change, or HA creates new entities. The keys are `print_file`, `timelapse`, `bed_leveling`, `start_print`, `reread_slot_1` to `reread_slot_4`, `cloud_sender`, `cloud_camera`, `current_print`, `chamber_light`, `queue`. |
| `platform` | string | `select`, `switch`, `button`, `sensor`, `camera`, `light`, `image` or `todo`. The integration skips a platform it does not know. |
| `name` | string | The English entity name. Set it as the entity name with `has_entity_name = True`. Do not set a `translation_key`. The entity id derives from it (`button.<device>_start_cloud_print`). |
| `icon` | string, optional | An `mdi:` icon. Only the re-read buttons have one. |
| `state` | `"local"` or `"app"`, optional | `local`: HA holds the state and restores it after a restart (select, two switches). `app`: the state comes from `GET /api/integration/state`. Buttons have no `state`. |
| `default` | bool, optional | Start value of a local switch when nothing can be restored. |
| `options` | object, optional | Sensor only. Its keys are the states (`idle`, `sent`, `error`) and the values their English labels. The state of the sensor is one of the keys. |
| `press` | object, optional | Button only, see below. |
| `command` | string, optional | Light only: the path to POST a switch to. `$serial` stands for the printer serial. |

The names of today's entities are fixed by this table: `Print file`, `Print timelapse`, `Print bed leveling`, `Start cloud print`, `Re-read slot 1` to `Re-read slot 4`, `Cloud sender`, `Cloud camera`, `Current print`, `Chamber light (cloud)`, `Queue`. A test compares them with `tests/golden/contract.json`. Existing entities keep their entity ids in the HA registry whatever the name does later. There are no translations: the integration has only English today.

### State: `GET /api/integration/state`

One answer holds everything the entities show. The integration polls it every 5 s (what the events poll did). One call asks the bridge for its printers (which includes the live camera state), HA for its states (cached 3 s) and the media folder (cached 5 s), so a faster poll gains nothing.

```json
{
  "manifest_revision": "a1b2c3d4e5f6",
  "printers": {
    "0000TEST0000001": {
      "name": "My H2S",
      "model": "H2S",
      "entities": {
        "print_file": {"options": ["benchy.gcode.3mf", "sub/cube.gcode.3mf"]},
        "cloud_sender": {"state": "sent", "attributes": {"account_email": "me@example.com", "last_file": "benchy.gcode.3mf",
                         "last_time": "2026-11-27T19:00:00+00:00", "last_error": null}},
        "cloud_camera": {"online": true, "stream": "/api/camera/0000TEST0000001/live", "transcode": false,
                         "snapshot": "/api/integration/printers/0000TEST0000001/snapshot"},
        "current_print": {"token": "3f2a9c01d4be", "file_path": "/media/druckwerk/benchy.gcode.3mf", "has_preview": true},
        "chamber_light": {"on": false},
        "queue": {"items": [{"uid": "a1b2c3d4e5f6", "summary": "Benchy",
                             "description": "File: benchy.gcode.3mf\nOwner: Anna (needs approval)"}]}
      }
    }
  }
}
```

- `manifest_revision` is the `revision` of the manifest. The integration reads the manifest again when it differs (the same job as `manifest_revision` in `GET /api/events`).
- `printers` has one key per printer the bridge knows (any HA account), not only the printers set up in the app. A printer that is not in the answer makes its entities unavailable. A failed call makes all entities of the integration unavailable.
- `name` and `model` (`dev_product_name`, else `dev_model_name`, else null) are for the device info, used only when no vendor device exists for the serial (the integration's own device).
- `entities` has one value per `app` entity and for `print_file`. The values per key are in the next section.

### What the integration does for each platform

Every entity is created for each serial in `printers`, now and when a new serial shows up later, with unique id `<serial>_<key>` and the name from the entity table. The device is the vendor's device for `(VENDOR_DOMAIN, serial)` when that integration has one; else the integration registers its own device with the identifier `(druckwerk, serial)`, `name`, `model`, `serial_number` and the vendor manufacturer. Entity ids do not depend on which of the two it is. `serial_fields` accept the id of either device.

**select `print_file`** (`state: local`). The options are `entities.print_file.options` (file names relative to `/media/druckwerk`, sorted, subfolders as `sub/name.gcode.3mf`). Selecting stores the option in HA only. After a restart, restore the last state when it is in the options. `current_option` is the stored value when it is in the options, else None; a value whose file vanished is remembered if it returns.

**switch `timelapse`, `bed_leveling`** (`state: local`). On and off change HA state only. Restore the last state after a restart, else `default`.

**button `start_print`**. On press: run `press` (below). The values `$print_file`, `$timelapse` and `$bed_leveling` are the local states of the select and the two switches of the same printer. The file is the option string as it is: the app resolves it against `/media/druckwerk`, so the integration no longer checks files or paths.

**button `reread_slot_1` to `reread_slot_4`**. On press: run `press`. It posts `{ams_id: 0, slot_id: <n - 1>}` to `/api/integration/printers/<serial>/rfid`.

How to run `press = {path, body?, needs?}`:
1. `needs` maps a local entity key to an error text. If that entity has no value (select without option), raise `HomeAssistantError(text)` and call nothing.
2. In `path`, replace `$serial` with the serial. In `body`, replace each string value that equals `$serial` with the serial and each string value that equals `$<key>` with the value of the local entity `<key>` (a string for the select, a boolean for a switch). All other values are literal.
3. `POST hub_url + path` with the body as JSON and `X-Hub-Secret`. When `path` starts with `/api/actions/` or `/api/tools/`, add `X-Druckwerk-Act-As: <context.user_id>` when there is one. Use a timeout of at least 400 s: a print send waits up to 300 s for the bridge.
4. Errors as in the first paragraph, then poll the state at once.

**sensor `cloud_sender`**. `native_value` is `entities.cloud_sender.state`, `extra_state_attributes` is `entities.cloud_sender.attributes` (`account_email`, `last_file`, `last_time`, `last_error`). Device class enum with the keys of `options` as options. The state is per printer and also survives an app restart.

**camera `cloud_camera`**. The entity is the live view plus stills.
- `online` is `entities.cloud_camera.online`. It is also the attribute `online`. Features: `STREAM` when online, else none.
- `stream_source()`: when online, `bridge_url + stream`; when `transcode` is true, `ffmpeg:<that url>#video=h264`. When offline, None. The stream path is a bridge route, so it uses `bridge_url`, not `hub_url`.
- `async_camera_image()`: `GET hub_url + snapshot` with `X-Hub-Secret` and a timeout of 120 s. Answer 200 with `image/jpeg` is the image. While the printer is offline, the app answers the placeholder picture at once, so the integration needs neither a local placeholder nor a check. Any other answer is a failure: keep the last image and set the attribute `last_error` to `HTTP <status>: <first 300 characters of the body>`.
- The integration keeps its own frame cache (serve the last image, refresh in the background after 5 s, wait for a new frame when the last one is older than 60 s) and the attributes `frame_time` (time of the last good frame) and `last_error`. They are HA-side behavior of the entity and have no app state.

**light `chamber_light`**. `is_on` is `entities.chamber_light.on` (true, false, or null for unknown). The app copies it from the printer's own report (`lights_report`, node `chamber_light`). It changes at the next poll, up to 5 s after a switch. Turn on and off: `POST hub_url + command` (with `$serial` replaced) and body `{"on": true|false}`; 200 `{"sent": 2}` or the error text. Color mode on/off only.

**image `current_print`**. Content type `image/png`. When `entities.current_print.token` changes, set `image_last_updated` to now and drop the cached image; when it is null, the last-updated value is None and `async_image()` returns None. `async_image()`: `GET hub_url + /api/integration/printers/<serial>/image` with the secret; 200 is a PNG, 404 means no picture. Attributes: `file_path` and `has_preview` from `entities.current_print`. The integration no longer follows the vendor's task sensor, finds files or restores `last_path`: the app does all that. The app chooses the file like this: the file named like the printer's reported task (`<task>.gcode.3mf`, top-level copy first), else the file of the last send; the picture is the slicer's own plate image from the file, else drawn from its G-code. The token changes with the file or with the file's modification time.

**todo `queue`**. Features: `DELETE_TODO_ITEM` and `MOVE_TODO_ITEM`. No create and no update. Items come from `entities.queue.items`, in order, each `TodoItem(uid=uid, summary=summary, description=description, status=NEEDS_ACTION)`. They are the waiting jobs (`queued` and `pending_approval`) with that printer as target or printer. Jobs for `any` are not listed.
- Delete one or more uids: for each uid `POST /api/queue/<uid>/cancel` with body `{}`. Then poll the state at once.
- Move `uid` after `previous_uid` (None means to the top): `waiting` is the uids of the current items without `uid`; `position` is 0 when `previous_uid` is None, else `waiting.index(previous_uid) + 1` when `previous_uid` is in `waiting`, else `len(waiting)`. Then `POST /api/queue/<uid>/reorder` with `{"position": position, "scope": <serial>}`. Then poll the state at once.
- Both calls go without `X-Druckwerk-Act-As`, as today (the todo list is for HA users who can see it). Errors are shown as `HomeAssistantError("Druckwerk: <text>")`. The cancel and reorder rules and answers are in "Queue".

### Print: `POST /api/actions/print_file`

The fields, names, defaults and `response: optional` are those of the manifest action `print_file` (golden contract). Body example: `{"serial": "0000TEST0000001", "file": "benchy.gcode.3mf", "timelapse": false, "bed_leveling": "on", "use_ams": true, "force": false}`. Answer: `200 {result: <the bridge's answer, for example {"result": "sent"}>}`.

- Serial: `serial`, else `device_id`. The integration replaces a device id by the serial before it forwards (`serial_fields: ["device_id"]`), and the app takes the value as the serial. Neither: `400 must contain at least one of device_id, serial.`
- File: a path relative to `/media/druckwerk`, or an absolute path under `/media` or `/share`. The real path (symlinks resolved) must stay inside. 400 texts: `Path <file> is outside the allowed folders (/media, /share)`, `Path <file> is outside the druckwerk media folder`, `Only .gcode.3mf files can be printed`, `File not found: <file>`.
- Busy check: the app reads the printer's `print_status` fresh from the bridge. `RUNNING` or `PAUSE` without `force` is `409 Printer <serial> is <STATUS>; refusing to send a new print`.
- G-code guard (`hub/gcode_guard.py`): not skipped by `force`. A file that would print air is `409 Refusing to send <file>: <problems>`. With `use_ams: false` only the AMS load check is skipped.
- Send: the app posts the file (base64, up to 64 MB, 300 s) to the bridge `POST /api/print` with `serial, filename, file_b64, timelapse, bed_leveling, use_ams` and the advanced options that were given. `on` and `off` for a tri option become `true` and `false`; `auto` stays. A bridge failure is `502` with the bridge's text (`error`, `message` or `detail`), else `Add-on answered HTTP <status>: <body>`; a bridge that does not answer is `502 Add-on not reachable: <reason>`.
- Result: every try sets the printer's result for the `cloud_sender` sensor: `result` `sent` or `error`, `last_file`, `last_time`, `last_error`, and after a success `last_path`. It is kept in `/data/sender.json`.
- After a success with `use_ams: true`, the app watches the print (see "Watchdog").
- The queue's own start and the web UI's `POST /api/jobs/<id>/print` call the same function inside the app. They do not call Home Assistant.
- No grant rule applies, as before with the HA service: any internal caller prints. The HA service `druckwerk.print_file` is registered from the manifest like the other actions and forwards here.

### Watchdog

After a send with `use_ams: true` the app reads the printer's status, stage and active tray from the bridge and pauses a print that runs without filament. It checks every 10 s (`CHECK_EVERY`). Active tray `none` with stage `printing` or `idle` for 120 s (`AIR_FOR`) while the status is `running` pauses the print once (`POST /api/pause/<serial>` on the bridge) and creates the persistent notification `druckwerk_air_<serial>` titled `Print without filament` in HA. A watch ends when the status leaves `running` (except `pause`), when a filament change finishes with a tray loaded, or after 15 min (`GIVE_UP_WAITING`) without `running`. A tick in which the bridge cannot say changes nothing. A watch is lost when the app restarts.

### Events and notifications

The app fires every event of `GET /api/events` on the HA bus as `druckwerk_<type>` (`druckwerk_job_done`, `druckwerk_job_started`, `druckwerk_job_failed`, `druckwerk_approval_needed`, `druckwerk_queue_changed`) with the event as data (`{seq, type, serial, job}`), through `POST /api/events/<type>` of the HA REST API. It sends them from its own thread, so a slow HA never holds up a request.

Notifications go to the recipients the integration pushed (next section):
- `job_done` with a next job waiting (the first queued job for that printer or for `any`, not a drying): `Plate clear? Start next: <job name or file>` with one button `{action: "DRUCKWERK_START_<serial>", title: "Plate clear, start"}`.
- `approval_needed`: `<owner name or "Someone"> needs approval to <print|dry> <job name or file>`, no button.
- Who gets it: a recipient whose person entity has state `home` and an attribute `user_id` that is an HA admin or the owner of that printer. Title `Druckwerk`; the call is `notify.<service>` with `{title, message, data: {actions}}`.
- The button: the app subscribes to the HA event `mobile_app_notification_action`. An action that starts with `DRUCKWERK_START_` starts the next job of that serial with `plate_clear` (the same as `POST /api/printers/<serial>/start-next` as an internal caller). A refusal is only logged.

The integration must remove its own firing of bus events, its notifications and its listener for `mobile_app_notification_action`, or everything happens twice. `GET /api/events` stays as it is.

### Options: `PUT /api/integration/options`

Body `{recipients: {<person entity id>: <notify service name>}}`, for example `{"recipients": {"person.anna": "notify.mobile_app_annas_phone"}}`. The service name may have the `notify.` prefix or not. Answer: `200 {recipients: {...}}` (what is stored). `400` when `recipients` is not an object of strings. The app stores it in `/data/integration.json` and reads it for every notification. The whole object is replaced on each push; an empty object turns notifications off. Until the first push no notification goes out.

The integration pushes at setup (also on every reload and when the app answers again after being down) and in its options update listener. The integration keeps `recipients` as its option (config flow) and the hub and upload secrets.

### Files: `GET /api/files`

`200 {files: [...]}`: the names of all `*.gcode.3mf` files (case-insensitive suffix) below `/media/druckwerk`, relative with `/`, subfolders included, sorted. The same list is in `entities.print_file.options`.

### Upload: `PUT /api/upload/<name>`

The integration keeps the HTTP view `/api/druckwerk/upload/<name>` (HA's IP ban hook is HA only) and the check of `X-Druckwerk-Secret` against its upload secret option (403 and 401 texts as today). When the secret is right it forwards: `PUT hub_url/api/upload/<name URL-encoded>[?overwrite=1]` with `X-Hub-Secret`, the body streamed as it comes and `Content-Length` set (the app answers `411 Content-Length is required.` without it, and does not read chunked bodies). It then passes the status and the answer on (map `{error}` to HA's `{message}`) and polls the state, so the select lists the new file.

Checks, in this order: name, size, content.
- Name must match `[A-Za-z0-9][A-Za-z0-9._ -]{0,118}\.gcode\.3mf` (after URL decoding), else `400 Name must be letters, digits, '._ -' and end in .gcode.3mf.`
- `Content-Length` above 64 MiB is `413 File too large (limit 67108864 bytes).` The app answers before it reads the body, so check `content_length` in the view first and answer 413 yourself.
- The body must be a zip with a `Metadata/plate_<n>.gcode` entry: `400 Body is not a 3MF (zip) file.` or `400 The 3MF has no sliced plate (Metadata/plate_N.gcode); export it as .gcode.3mf.`
- An existing file without `?overwrite=1` is `409 File exists; add ?overwrite=1 to replace it.`

The file is stored as `/media/druckwerk/<name>` (mode 0644, replaced atomically). Answer `200 {file: <stored name>, bytes: <size>, sha256: <hex>}`.

### Picture and camera still

- `GET /api/integration/printers/<serial>/image`: `200 image/png`, or `404 {error: "no preview"}`.
- `GET /api/integration/printers/<serial>/snapshot`: `200 image/jpeg`. Online printer: the bridge's snapshot is passed through with its `X-Snapshot-Age` header, with the bridge's status and text on failure (it waits up to 90 s). A camera timeout answers 502. It does not restart the bridge daemon by itself: the bridge restarts it only if a pushall probe gets no status change in 15 s. Offline printer: the placeholder picture. `404` for a serial the bridge does not know.

### Commands

- `POST /api/integration/printers/<serial>/light` with `{"on": true|false}` (else 400 `Body must be {"on": true|false}.`) switches the chamber light through the bridge (`POST /api/light/<serial>`). Answer `{"sent": 2}`.
- `POST /api/integration/printers/<serial>/rfid` with `{"slot_id": 0-3, "ams_id": 0-7}` (`ams_id` default 0) makes the AMS read that slot's tag again, like the button did. Answer `{"sent": 1}`.
- Both pass the bridge's refusal on: 400, 404 and 409 with its text, other failures as 502.

### What stays in the integration

- Config flow, options flow and the options `hub_url`/`url`, hub secret, upload secret and `recipients`. Pushing `recipients` (above).
- Device handling: the link to the vendor device (`VENDOR_DOMAIN`, serial) when there is one, its attach logic and the integration's own device as the fallback. The resolution of a device id to a serial (`serial_fields`) and the HA user id for `X-Druckwerk-Act-As`.
- The restore of state for the select and the two switches, and the HA entity classes themselves, built from the entity table.
- The upload view with the secret check and the IP ban, as a forwarder.
- Registering the HA services and the LLM API from the manifest, and the repair issue for a higher `api_version`.
- The camera frame cache and the attributes `frame_time` and `last_error`.

### What the app owns now

The G-code guard, the plate picture, the print send with its busy check, the per-printer send result, the watchdog, the notifications and the bus events, the notification button, the file list and path rules, the upload checks, and the entity state. The integration can delete `gcode_guard.py`, `preview.py`, `watchdog.py`, `upload.py` checks, `api.scan_files`, `api.resolve_file`, `api.read_b64`, `api.plate_png`, `coordinator.async_print`, the notification code of `hub.py`, `offline.jpg` and the entity names of `strings.json`.

## Plates inbox (agents push a sliced plate, the app queues it)

For agents that cannot reach Home Assistant. The app option `plates_token` (a GitHub token with Contents read and write) turns it on; `plates_repo` names the private repo (default `marionebl/druckwerk-plates`).

An agent pushes to the default branch of that repo:

- `plates/<id>/<name>.gcode.3mf`: a sliced plate. `<id>` is a new folder name that was never used before (letters, digits, `.`, `_`, `-`, for example a date and a random suffix). One folder per upload. Files up to 100 MB.
- `plates/<id>/job.json` (optional): `{"printer": "<serial>", "model": "H2S", "name": "Bracket v2", "owner": "<HA user id or name>"}`. All fields are optional. `printer` sends the job to that printer (the file's model must match). Without it, `model` or the model in the G-code header picks any printer of that model. `name` is the queue name (at most 80 characters). `owner` must be an active HA user who has a grant on the printer, otherwise the printer's owner gets the job.
- Push the plate and `job.json` in one commit. If you use the contents API, upload `job.json` first and the plate last.

The app checks `plates/` every 60 s. A plate that passes the checks (sliced, G-code guard, printer model) is added to the queue and waits there for a person or an automation to start it. Then the app deletes the folder from the repo. A plate that fails a check stays in the repo with no job; the reason is in the Setup view and the app log. Fix it by pushing a new folder with a new id.
