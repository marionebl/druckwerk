"""Identifiers owned by the printer vendor or its Home Assistant integration: the only place that names them.

Everything else in the repository is our own code. Shell scripts read the same values from externals.env
(hub/tests/test_externals.py keeps the two in step). Values must stay exact: they are what the vendor's software expects.
"""

# -- network plugin: downloaded at app start, never shipped in the image
PLUGIN_VERSION = "02.05.00.56"  # version the bridge was tested with
PLUGIN_URL = "https://public-cdn.bblmw.com/upgrade/studio/plugins/02.05.00.56/8b2ace6050/linux_02.05.00.56.zip"  # the vendor's CDN
PLUGIN_SHA256 = "2450272e480abe8f52e13cc5814eafb095ddd24c420240c611c7d5fe6a6a4567"  # of the zip above
PLUGIN_NET_LIB = "libbambu_networking.so"  # what the bridge loads
PLUGIN_LIBS = (PLUGIN_NET_LIB, "libBambuSource.so", "libagora_rtc_sdk.so", "libagora-fdkaac.so", "liblive555.so")  # the zip's five libs
PLUGIN_HOST_LINKED = PLUGIN_LIBS[:2]  # the bridge's existence check runs outside qemu's -L prefix and needs host-side paths
PLUGIN_FIND_PATTERNS = ("libbambu*", "libBambuSource*", "libagora*", "liblive555*")  # image check: none may exist
BRIDGE_LIB_ENV = "BAMBU_LIB_PATH"  # the bridge reads the plugin path from this variable
PLUGIN_AGENT_DIR = "/tmp/bambu_agent"  # hardcoded working dir of the plugin
CERT_URL = "https://raw.githubusercontent.com/bambulab/BambuStudio/master/resources/cert/slicer_base64.cer"  # AGPL repo, the bridge's signing cert

# -- cloud API, as the vendor's own slicer calls it (the cloud checks the client name)
CLOUD_API_BASE = "https://api.bambulab.com"
CLOUD_HEADERS = {
    "X-BBL-Client-Name": "OrcaSlicer",
    "X-BBL-Client-Type": "slicer",
    "X-BBL-Client-Version": "02.05.00.00",
    "User-Agent": "bambu_network_agent/02.05.00.00",
    "Content-Type": "application/json",
}
CLOUD_NAME = "Bambu"  # shown in error messages
CLOUD_CLIENT_HEADER = "X-BBL-Client-Name"  # the mock cloud in tests reads it

# -- printer models the browser slicer supports (preset names are those of the engine's flattened presets, verbatim)
PRINTERS = {  # dev_product_name -> machine preset, bed type and process presets per quality (0.4 mm); "nozzles" has the others
    "H2S": {"preset": "Bambu Lab H2S 0.4 nozzle", "bed": "High Temp Plate", "model_id": "O1S",
            "quality": {"good": "0.24mm Standard @BBL H2S", "better": "0.20mm Standard @BBL H2S",
                        "best": "0.16mm High Quality @BBL H2S"},
            "nozzles": {
                "0.2": {"preset": "Bambu Lab H2S 0.2 nozzle",
                        "quality": {"good": "0.12mm Balanced Quality @BBL H2S 0.2 nozzle", "better": "0.10mm Standard @BBL H2S 0.2 nozzle",
                                    "best": "0.08mm High Quality @BBL H2S 0.2 nozzle"}},
                "0.6": {"preset": "Bambu Lab H2S 0.6 nozzle",
                        "quality": {"good": "0.30mm Standard @BBL H2S 0.6 nozzle", "better": "0.24mm Balanced Quality @BBL H2S 0.6 nozzle",
                                    "best": "0.18mm Balanced Quality @BBL H2S 0.6 nozzle"}},
                "0.8": {"preset": "Bambu Lab H2S 0.8 nozzle",
                        "quality": {"good": "0.40mm Standard @BBL H2S 0.8 nozzle", "better": "0.32mm Balanced Quality @BBL H2S 0.8 nozzle",
                                    "best": "0.24mm Balanced Quality @BBL H2S 0.8 nozzle"}}}},
    "P1S": {"preset": "Bambu Lab P1S 0.4 nozzle", "bed": "Textured PEI Plate", "model_id": "C12",
            "quality": {"good": "0.24mm Draft @BBL X1C", "better": "0.20mm Standard @BBL X1C",
                        "best": "0.16mm High Quality @BBL X1C"},
            "nozzles": {
                "0.2": {"preset": "Bambu Lab P1S 0.2 nozzle",
                        "quality": {"good": "0.12mm Standard @BBL X1C 0.2 nozzle", "better": "0.10mm Standard @BBL X1C 0.2 nozzle",
                                    "best": "0.08mm High Quality @BBL X1C 0.2 nozzle"}},
                "0.6": {"preset": "Bambu Lab P1S 0.6 nozzle",
                        "quality": {"good": "0.36mm Standard @BBL X1C 0.6 nozzle", "better": "0.30mm Standard @BBL X1C 0.6 nozzle",
                                    "best": "0.18mm Standard @BBL X1C 0.6 nozzle"}},
                "0.8": {"preset": "Bambu Lab P1S 0.8 nozzle",
                        "quality": {"good": "0.48mm Standard @BBL X1C 0.8 nozzle", "better": "0.40mm Standard @BBL X1C 0.8 nozzle",
                                    "best": "0.32mm Standard @BBL X1C 0.8 nozzle"}}}},
}

# -- the vendor's Home Assistant integration (ha-bambulab): registry domain of its devices and entities
# (the integration in custom_components/ has its own copy, see custom_components/druckwerk/externals.py)
HA_VENDOR_DOMAIN = "bambu_lab"

# -- G-code written by the vendor's slicers: `; printer_model = Bambu Lab H2S`, lower-cased with spaces removed
GCODE_MODEL_PREFIX = "bambulab"
