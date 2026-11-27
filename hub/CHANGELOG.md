# Changelog

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
