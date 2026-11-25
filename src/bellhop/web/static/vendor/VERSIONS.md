# Vendored Browser Media Libraries (`src/bellhop/web/static/vendor/`)

Bellhop vendors its browser MSE/HLS media playback libraries directly in the
repository so `bellhop serve` runs completely offline on a LAN without third-party
CDNs and satisfies the strict `Content-Security-Policy` (`script-src 'self' blob:`).

## Pinned Artifacts

| File | Library | Version | License | Upstream | Bytes | SHA-256 |
| --- | --- | --- | --- | --- | --- | --- |
| `mpegts.js` | `mpegts.js` | `1.7.3` | Apache-2.0 | https://github.com/xqq/mpegts.js | `217829` | `b83dccba1525ae65763b373a85a6513f0b533359292a90083b25b29977a75f90` |
| `hls.min.js` | `hls.js` | `1.5.17` | Apache-2.0 | https://github.com/video-dev/hls.js | `413952` | `484054e8cd03d3f6d1781fb7f402bdc318d8a4c527f933a95c624e27cc9a9470` |

## License Notices

- **`mpegts.js` (`1.7.3`)** — Copyright (C) xqq and contributors. Licensed under
  the Apache License, Version 2.0 (`https://github.com/xqq/mpegts.js/blob/master/LICENSE`).
- **`hls.js` (`1.5.17`)** — Copyright (C) Dailymotion and `hls.js` contributors.
  Licensed under the Apache License, Version 2.0 (`https://github.com/video-dev/hls.js/blob/master/LICENSE`).

## Upgrade Procedure

1. Download the official release artifact (`dist/mpegts.js` or `dist/hls.min.js`)
   from the upstream GitHub release tag.
2. Compute its byte size and SHA-256 digest:
   ```bash
   sha256sum src/bellhop/web/static/vendor/mpegts.js src/bellhop/web/static/vendor/hls.min.js
   wc -c src/bellhop/web/static/vendor/mpegts.js src/bellhop/web/static/vendor/hls.min.js
   ```
3. Update the table above with the new version, byte size, and SHA-256 hash.
4. Verify global TypeScript ambient declarations in
   `src/bellhop/web/frontend/types.d.ts` (`MpegtsStatic`, `HlsStatic`) still
   match the library APIs and run `.venv/bin/pytest -v`.
