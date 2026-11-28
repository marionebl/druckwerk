"""The steps between Start and the first layer, as one list the UI draws: upload to the printer cloud (from the bridge),
the printer's download of the plate, its preparation (stage names), then printing. Pure, so tests cover it without a printer."""

# Stages the printer reports before the first layer (printer_state.STAGES names).
PREPARE_STAGES = ("auto_bed_leveling", "heatbed_preheating", "sweeping_xy_mech_mode", "changing_filament", "heating_hotend",
                  "calibrating_extrusion", "scanning_bed_surface", "identifying_build_plate_type", "calibrating_micro_lidar",
                  "homing_toolhead", "cleaning_nozzle_tip", "checking_extruder_temperature", "calibrating_extrusion_flow",
                  "filament_unloading", "filament_loading", "calibrating_motor_noise")
UPLOAD_TEXT = {"create": "Preparing the upload", "upload": "Uploading", "waiting": "Waiting for the cloud",
               "sending": "Sending the job", "record": "Sending the job", "waitprinter": "Waiting for the printer"}


def steps(sending, upload, v):
    """sending: the hub is posting a file to the bridge; upload: the bridge's {stage, percent} for it (or None);
    v: printer_state.view. Returns None when no start is under way, else [{id, state, percent, detail}] with state done|active|pending."""
    v = v or {}
    status, stage = v.get("status"), v.get("stage")
    if not sending and status == "prepare":
        pre = True
    else:
        pre = not sending and status == "running" and stage in PREPARE_STAGES
    if not sending and not pre:
        return None
    pct = v.get("prepare_percent")
    downloading = pre and stage not in PREPARE_STAGES and (pct is None or pct < 100)
    if sending:
        active = "upload"
    elif downloading:
        active = "download"
    else:
        active = "prepare"
    order = ["upload", "download", "prepare", "print"]
    out = []
    for sid in order:
        state = "active" if sid == active else "done" if order.index(sid) < order.index(active) else "pending"
        percent = detail = None
        if sid == "upload" and sending and upload:
            percent = upload.get("percent") if upload.get("stage") == "upload" else None
            detail = UPLOAD_TEXT.get(upload.get("stage"))
        elif sid == "download" and active == "download":
            percent = pct
        elif sid == "prepare" and active == "prepare" and stage in PREPARE_STAGES:
            detail = stage
        out.append({"id": sid, "state": state, "percent": percent, "detail": detail})
    return out
