# fengling CLI

Composable Mac CLI for the Fengling / Suno recut automation.

The first version wraps the migrated `suno_auto_recut_upload.py` script and is intended to run natively on macOS.

## Install

```bash
make install-local
command -v fengling
```

## Configure

For the migrated Mac copy:

```bash
fengling --json config init --app-root ~/fengling-studio
fengling --json deps install
fengling --json deps install --execute
fengling --json doctor
```

## Command Surface

```bash
fengling --json doctor
fengling --json config show
fengling --json runs list --limit 5
fengling --json runs get 20260706_111254
fengling --json logs list --limit 5
fengling --json logs tail --lines 60
fengling --json deps install
fengling --json browser preheat
fengling --json browser preheat --execute
fengling --json run upload /path/to/audio.wav --title "Demo"
fengling --json run upload /path/to/audio.wav --title "Demo" --execute
fengling --json raw script -- --scan-uploaded-clips
```

Write-like commands preview by default. Use `--execute` to run the underlying script.

## JSON Policy

With `--json`, successful commands return an envelope:

```json
{
  "ok": true
}
```

Errors are machine-readable and redact secrets:

```json
{
  "ok": false,
  "error": {
    "code": "run_not_found",
    "message": "Run not found",
    "details": {}
  }
}
```

The CLI never prints Suno tokens. `doctor --json` reports Python packages, Chrome/Edge presence, ffmpeg, and the migrated app root.

## Safety

- `run upload`, `browser preheat`, and `raw script` are preview-only unless `--execute` is present.
- Old product-gating code has been removed from the migrated backend, not merely bypassed.
- The old Fengling private service is not required for upload/render CLI workflows.
- Raw script writes should be treated as live actions once `--execute` is used.
