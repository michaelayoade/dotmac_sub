# Brand assets (white-label)

Per-organization image assets for the mobile app. These are build-time assets:
each organization supplies its own and rebuilds.

## `login_logo.png`

Optional. If present, it is shown on the login screen in place of the default
placeholder icon (`Icons.wifi`). If absent, the login screen falls back to the
icon automatically (via `Image.asset(..., errorBuilder: ...)`), so the app still
builds and runs without a logo.

- Recommended: a square or wide PNG with transparent background.
- Rendered at ~72px tall on the login screen.

No other configuration is needed — `assets/images/` is already declared in
`pubspec.yaml`, so dropping the file here and rebuilding is enough.
