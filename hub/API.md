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
  `suggested_prefix` is the printer integration's entity prefix for that serial: the device whose identifiers contain the serial, then the entity registry's `<serial>_print_status` entity minus `sensor.` and `_print_status`; falls back to the slug of the device name; null if HA has no such device.
  `config` is added to each printer: `null`, or the saved printer (shape of the `PUT` answer) for editing.
- `POST /api/setup/login/start` `{email, password}` → `{state: "done"|"need_code"|"need_tfa", account}`
- `POST /api/setup/login/code` `{email, code}` → same
- `DELETE /api/setup/accounts/{id}`
- `PUT /api/setup/printers/{serial}` `{name, ha_prefix, plate, nozzle, pause_entities: [entity_id], electricity_kwh_price?: number}` → `{serial, name, model, account, ha_prefix, plate, nozzle, pause_entities, electricity_kwh_price}`.
  `model` and `account` come from the bridge; 400 if HA has no `sensor.<ha_prefix>_print_status`, 404 if no bridge account has the serial.
- `DELETE /api/setup/printers/{serial}`
- `GET /api/integration` → `{url, secret, connected}` (HA admins; fallback for the config flow, same data as the discovery message). `connected` is true when HA has a `druckwerk` config entry (read over the HA websocket, false on error).
- Printers register themselves: for each printer the bridge lists, with a `suggested_prefix` whose `sensor.<prefix>_print_status` exists in HA, the app saves default settings. It runs at start, on `GET /api/setup` and every 60 s. A printer removed with `DELETE /api/setup/printers/{serial}` is not registered again until a later `PUT` for it.
- `/api/bridge/*` proxies the bridge's own page and API (HA admins only).

## Printers (filtered to printers the caller has an active grant on; HA admins see all)

- `GET /api/printers` → `[{serial, name, model, role, online, status, stage, progress, remaining_min, end_time, layer, total_layers,
  nozzle_temp, nozzle_target, bed_temp, bed_target, chamber_temp, task_name, cover_url, camera_url,
  active_tray: {unit, slot} | null,
  ams: [{unit, humidity_index, temp, trays: [{slot, type, colour: "RRGGBB", name, remain_pct, spool_id}]}],
  external: {type, colour, name} | null,
  hms: [{code, severity: "fatal"|"serious"|"common"|"info", text, url}],
  drying: [{ams_id, name, status, active, left_min, filament, temp, hours, temp_min, temp_max}],
  current_job: job | null, queue_length}]`
  Values come from the printer integration's entities, bridge status as fallback when a printer has no `print_status` entity. `progress` is a number 0..100, `remaining_min` whole minutes, temps numbers; unavailable values are null.
  `active_tray` and tray `slot` are 0-based, `unit` is the AMS number (0 = "AMS 1"); trays and humidity come from the AMS devices in the entity registry. `humidity_index` is the printer integration's 1 (dry) .. 5 (wet). Empty slots have `type: ""`. `spool_id` is the spool assigned to that tray, or null. `camera_url` and `cover_url` are ingress-relative.
- `drying` lists the AMS units that have a dryer (AMS 2 Pro, AMS HT) and whose printer supports remote drying (bridge `GET /api/drying/{serial}`, cached 5 s); `[]` when the bridge cannot say. `ams_id` is the AMS number as the printer uses it (0 to 3, 128 and up for an AMS HT), `status` is `off|checking|drying|cooling|stopping|error|testing`, `active` is true while the AMS is drying, `left_min` the minutes left.
- `GET /api/printers/{serial}/camera` → MJPEG stream of the cloud camera (bridge `/api/camera/{serial}/live`, query string passed on); with `?mode=snapshot` one JPEG (`/snapshot`). The web UI only uses snapshots, because H.264 printers stream MPEG-TS, which an `<img>` cannot show. Needs any active grant on the printer.
- `GET /api/printers/{serial}/cover` → the print's cover image (HA `image.<prefix>_cover_image`). Needs any active grant.
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
- `POST /api/queue/{id}/{reorder|move|cancel|retry|approve|reject|skip|stop}`. `stop` ends a running drying job and stops the AMS (admin of the printer; 409 if the job is not drying; the job stays when the bridge cannot stop it). `cancel` is for waiting jobs. A drying job cannot move to another printer. Drying jobs take part in the order of the printer's queue: the next job for a printer is the first waiting job, a print or a drying. While a drying runs (`drying`), no job starts on that printer. The job ends as `done` when the AMS stops reporting a drying (checked every 10 s, not in the first 2 minutes) or, when the state is unknown, 10 minutes after the set time. Drying jobs raise `approval_needed` but no `job_started|done|failed` events and use no spools.
  `reorder` takes `{position, scope?: serial}`: with `scope`, `position` is 0-based among the waiting (queued + pending_approval) jobs whose target or printer is that serial (other jobs keep their slots); without it, global. Access rules are unchanged: a user's reorder only permutes their own slots.
- `GET /api/queue/{id}/plate.png` → the job's plate image (kept with the job after the slice is pruned); visible to whoever sees the job.
- The job also carries `filament_grams` (per filament), `print_seconds` (slice estimate), `spools_used` (`[{spool_id, grams}]`, after done).

## History (hub/history.py)

- `GET /api/history?user=&printer=&limit=` → `{items, unavailable}`. The Bambu cloud's task list is the source of truth (per printer, via the bridge's `GET /api/tasks?serial=&limit=<=100`, cached 60 s). Druckwerk jobs only say who started a task: same printer, same file or title name (extension and case ignored) and the task started 60 s before to 15 min after the job; the closest start wins, each task and job once.
- item = `{id, serial, printer_name, title, status, status_code, started_at, ended_at, seconds, grams, cover_url, plate, design_title, job_id, user_id, user_name, cost, source}`; times are epoch seconds. `source` is `druckwerk` (a job matched) or `cloud` (started at the slicer or printer; `user_id`, `user_name`, `job_id`, `cost` are null). `status` is `finished|failed|cancelled|running`, or the raw cloud value when it is not known; `status_code` is always the raw value.
- Newest first. HA admins and printer owners see all items of their printers and may filter by `user` (an HA user id or `unknown`) and `printer`. Other users see only items attributed to themselves; `user` is ignored for them. `unavailable` lists serials whose cloud answer failed (items of the others still come).
- Bridge: `GET /api/tasks?serial=<serial>&limit=<1..100, default 50>` uses the token of the account that owns the printer; 404 unknown printer, 502 when the cloud fails or refuses the token.

## Slicing

- Existing slicer routes: `POST /api/jobs` (upload), `POST /api/jobs/{id}/slice`, `GET /api/jobs/{id}` (a done slice also carries `cost_estimate`: filament from the matching spool plus electricity, same formula as the final job cost; `null` when it cannot be read), `GET /api/jobs/{id}/(plate.png|plate.gcode)`, `POST /api/jobs/{id}/print` (direct print, admin of the printer only; the queue is the normal path).
  A slice belongs to the user who uploaded it (`owner_user_id`); others get 403, HA admins and the integration may use any. Slicing needs an active grant on the chosen printer.
- `POST /api/jobs/{id}/gcode?serial=&quality=&tray=&bed=&filament=&seconds=&grams=&metres=&engine=&slice_s=` (body: G-code): G-code sliced in the browser, packed as a `.gcode.3mf` and stored like a slice (no plate image: `info.has_png` is false). Same identity, grant (403 `rule: slicing needs an active grant on the printer`) and owner rules as `/slice`. `GET /api/engine/filament?serial=&tray=` → `{filament}`, the preset the browser slices a tray with (grant needed). `GET /engine/...` serves the WASM engine and the flattened presets.
- `POST /api/jobs/{id}/queue` `{target: serial|"any", model?, options?, name?}` → queue job (file, grams and plate image taken from the slice). The 3MF is copied into the share under a unique name; the add follows the normal grant rules (403, or `pending_approval`).

## Filament and cost

- `GET /api/spools` (spools on printers the caller has a grant on; unassigned spools only for HA admins) → `[{id, brand, type, colour, name, grams_left, grams_total, price_per_kg, printer?, unit?, slot?, source?: "seen", uid?, loaded_at?}]`
  The hub records the filament it sees: every 60 s it reads the AMS trays and, for a loaded tray that has no spool, creates a spool with `source: "seen"` (type, colour, name and remaining percent of the tray; `uid` is the RFID tag or tray uuid when the tray has one). A spool with the same `uid` moves with its tag; a spool made by a person that matches type and colour and is not loaded is reused (only its place changes). A seen spool whose tray is emptied stays on the list, not loaded. People's spools are never deleted or changed otherwise.
- `POST /api/spools`, `PUT /api/spools/{id}`, `DELETE /api/spools/{id}` (admin of the spool's printer, or HA admin; unassigned spools: HA admins). An assigned spool needs `unit` and `slot`. A `PUT` may unlink a spool (`printer: null`).
- A job's `grams` comes from the sliced 3MF (`Metadata/slice_info.config` filament used_g per filament). On `done` (exactly once) the grams are taken off the spool in the tray the slice was made for, else a spool of the same type and colour on that printer; never below 0.
  `cost` = sum(grams × price_per_kg / 1000) + (print hours × printer watt estimate / 1000 × electricity_kwh_price), rounded to cents. `print hours` is the slice's estimate. Watt estimate per model in code (H2S, X1, P1S, P1P, A1, A1 mini). A filament without a matching spool, or whose spool has `price_per_kg: null` ("no price", allowed), costs 0. The queue copy of the 3MF is deleted when the job is done.

## Access

- `GET/POST /api/grants`, `DELETE /api/grants/{id}` (HA admins; GET also internal). Answers carry `user_name` (HA user name) and `active` (until/condition evaluated now). Grant: `{id, user_id, user_name, printer: serial|"*", role: "owner"|"user", until?, condition_entity?, needs_approval?, active}`.
- `GET /api/ha/users` → `[{id, name, is_admin}]` (HA admins and internal), from HA `config/auth/list`; system and inactive users are left out.
- `GET /api/ha/entities?domains=schedule,input_boolean,binary_sensor,person` → `[{entity_id, name, state}]` (HA admins), for the condition picker.
- `POST /api/ha/switches` `{user_id, printer: serial|"*"}` → `{entity_id, name, state: "off"}` (HA admins, like grants). Creates an `input_boolean` helper through the HA websocket (`input_boolean/create`, icon `mdi:printer-3d`) named `Druckwerk <user name> <printer name or All printers>`. The entity may be missing from `/api/ha/entities` for a moment; a grant accepts it as `condition_entity` anyway. 400 for an unknown user or printer.
- `POST /api/feedback` `{item, description}` → `{ok: true}` (any HA user; the body may be up to 1500000 bytes, other routes allow 8 KB). Calls HA `todo.add_item` on `todo.feedback`; `item` is 1 to 300 characters, `description` a non-empty string (JSON text with the context and a screenshot). 400 for a bad body. Sent by the feedback button (`hub/ui/feedback.js`).

## Home Assistant restarts

While Home Assistant restarts (HTTP 502, 503, 504 or no answer), a route that needs it answers `503 {error: "Home Assistant is restarting. This page tries again in a few seconds.", retry: true}`. The web UI shows that text in a banner and asks `GET /api/me` with a growing pause (2 s up to 20 s) until it works, then draws the view again.

## Events for the integration

- `GET /api/events?since=<seq>` (internal) → `{seq, events: [{seq, type: "job_done"|"job_failed"|"job_started"|"approval_needed"|"queue_changed", serial, job}]}`.
  Without `since` the answer is `{seq: <current>, events: []}` (no backlog). `job` is the full job object (always with `id, file, owner_name, target, printer`; `printer` is null until a printer took it); `serial` is the job's printer or fixed target, `""` for `queue_changed` (then `job` is null) and for `any` jobs not yet placed.
  `job_started` fires when the print was sent, `job_failed` when a started job failed. Events are kept in memory (last 500); `seq` starts at the boot time in seconds, so it keeps growing across restarts.
  The integration polls this every 5 s and fires the notifications.
