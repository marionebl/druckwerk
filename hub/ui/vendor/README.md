# Vendored browser libraries

Both files are unmodified upstream builds. Licences sit next to them.

| File | Library | Version | Source | SHA-256 | Licence |
|---|---|---|---|---|---|
| `three.module.min.js` | three.js | r170 | https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.min.js | `08fd7545d13d2c7fb65ab691530a802dafefd638596501854f267d0fb13c39e7` | MIT (`LICENSE-three.txt`) |
| `mpegts.js` | mpegts.js | 1.8.0 | https://cdn.jsdelivr.net/npm/mpegts.js@1.8.0/dist/mpegts.js | `cfad10efe8305418493fb01e01c157f6f54a52c6c09b3ee02feae6fb93d8459a` | Apache-2.0 (`LICENSE-mpegts.txt`) |

Version strings are read from the files (`const t="170"` in three, `"1.8.0"` in mpegts). The source URLs are the
standard npm CDN paths of those versions; the sandbox had no network to confirm that the hashes match the CDN files.
To update a file, replace it, change the row and run `sha256sum` on it.
