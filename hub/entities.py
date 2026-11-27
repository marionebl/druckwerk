"""The entities the integration shows per printer (contract: hub/API.md). It builds them from this table."""

SLOTS = 4  # one AMS (ams_id 0) per printer today
PRINTER = "/api/integration/printers/$serial"

ENTITIES = [
    {"key": "print_file", "platform": "select", "name": "Print file", "state": "local"},
    {"key": "timelapse", "platform": "switch", "name": "Print timelapse", "state": "local", "default": False},
    {"key": "bed_leveling", "platform": "switch", "name": "Print bed leveling", "state": "local", "default": True},
    {"key": "start_print", "platform": "button", "name": "Start cloud print",
     "press": {"path": "/api/actions/print_file",
               "body": {"serial": "$serial", "file": "$print_file", "timelapse": "$timelapse",
                        "bed_leveling": "$bed_leveling", "use_ams": True},
               "needs": {"print_file": "Select a print file first"}}},
    *[{"key": "reread_slot_%d" % n, "platform": "button", "name": "Re-read slot %d" % n,
       "icon": "mdi:nfc-search-variant",
       "press": {"path": PRINTER + "/rfid", "body": {"ams_id": 0, "slot_id": n - 1}}}
      for n in range(1, SLOTS + 1)],
    {"key": "cloud_sender", "platform": "sensor", "name": "Cloud sender", "state": "app",
     "options": {"idle": "Idle", "sent": "Sent", "error": "Error"}},
    {"key": "cloud_camera", "platform": "camera", "name": "Cloud camera", "state": "app"},
    {"key": "current_print", "platform": "image", "name": "Current print", "state": "app"},
    {"key": "chamber_light", "platform": "light", "name": "Chamber light (cloud)", "state": "app",
     "command": PRINTER + "/light"},
    {"key": "queue", "platform": "todo", "name": "Queue", "state": "app"},
]
