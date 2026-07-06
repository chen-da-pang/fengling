---
name: fengling
description: Use the Mac-native Fengling CLI to inspect, test, or run the migrated Suno recut/upload/render automation.
---

# Fengling

Use the installed `fengling` CLI as the durable command surface for this project.

Start every session with:

```bash
command -v fengling
fengling --json doctor
```

First-run setup:

Before the first real run on a new Mac, tell the user Fengling needs these local pieces:

- `fengling` CLI installed and configured to `~/fengling-studio`.
- Python runtime used by the CLI.
- Python packages: `numpy`, `requests`, `websocket-client`.
- `ffmpeg` for decoding/splitting audio.
- Google Chrome.
- OpenCLI, because the macOS workflow reuses the user's logged-in Suno browser state through the `fengling-suno` session.

Check what is already present first:

```bash
command -v fengling
fengling --json doctor
command -v opencli
opencli doctor
command -v ffmpeg
```

If something is missing, explain the missing pieces in plain language and ask whether the user wants the model to install them on their behalf before running install commands. Do not silently install external tools such as OpenCLI, Homebrew packages, Chrome, or ffmpeg on a first-run setup. Installing local Python dependencies through `fengling --json deps install --execute` also requires user approval when it is part of first-run setup.

If config is missing or wrong, set the local Mac app root:

```bash
fengling --json config init --app-root ~/fengling-studio
```

If `doctor` reports missing Python dependencies, preview first, then install:

```bash
fengling --json deps install
fengling --json deps install --execute
```

For safe inspection:

```bash
fengling --json runs list --limit 5
fengling --json logs list --limit 5
fengling --json logs tail --lines 120
```

For login/browser testing:

```bash
fengling --json browser preheat
fengling --json browser preheat --execute
```

The macOS migration is expected to reuse the logged-in OpenCLI browser session:

- Default browser session: `fengling-suno`.
- The CLI sets `SUNO_USE_OPENCLI_BROWSER=1` and should prefer the user's existing Suno login state.
- Do not ask the user to log in again when the OpenCLI Chrome/Suno session is already logged in.

For upload testing, a dry-run is optional and mainly for debugging command construction:

```bash
fengling --json run upload /path/to/audio.wav --title "Test Song" --no-render
```

When this skill is invoked for a Fengling task, treat that as approval to run the actions needed for the stated task, matching the old app behavior. Do not ask for an extra confirmation before browser preheat, upload, render, cleanup, or other built-in workflow actions that are part of the user's request. Use dry-run first only when the task is exploratory, ambiguous, or debugging command construction.

```bash
fengling --json run upload /path/to/audio.wav --title "Test Song" --no-render --execute
fengling --json run upload /path/to/audio.wav --title "Render Test" --execute
```

After any completed live upload/render run, clean Suno Library intermediate clips by default.
The old app behavior should leave the final rendered song, not the uploaded slice clips.
Use the run's `uploaded_rows.json` first when available:

```bash
fengling --json raw script --execute -- --delete-uploaded-clips /path/to/run/uploaded_rows.json
```

Then scan the live Suno Library for remaining upload slice clips and delete those too:

```bash
fengling --json raw script --execute -- --delete-uploaded-clips /path/to/nonexistent-current-library-scan.json
fengling --json raw script --execute -- --scan-uploaded-clips
```

The final scan should report `total: 0`. This cleanup is part of the normal Fengling run finish, not an optional follow-up. It must not delete the final rendered song URL/result.

Studio state fallback:

- Suno may create a Studio project without returning a `state` field from `/api/studio/project/{project_id}`.
- The backend already supports a local fallback template at `~/fengling-studio/evidence/studio_blank_state_template.json`.
- If that template is missing or stale, open `https://suno.com/studio` in the OpenCLI `fengling-suno` session and extract the blank Studio `currentState` from the page, then save it as:

```bash
~/fengling-studio/evidence/studio_blank_state_template.json
```

- A valid template has at least `state.tracks` and `state.timing`; the verified blank Studio template had one audio track and `bps=2`.

Rules:

- Always prefer `--json` for analysis.
- Treat `browser preheat --execute`, `run upload --execute`, and `raw script --execute` as live actions.
- The migrated Mac backend has no card-key/license gate; do not reintroduce old product-gating logic.
- The old Fengling private service is not part of upload/render CLI workflows.
- Use `raw script` only when high-level commands are missing.
- Running this skill is explicit approval for the built-in live actions necessary to complete the user's Fengling task.
- Stay within the task the user gave; do not invent unrelated cleanup, deletion, publishing, or account-switching work.
- Do not add silent placeholder audio or copyright-silence workarounds. Preserve the original split/upload/retry/assembly logic unless the user explicitly asks to redesign it.
- Do not treat a successful `--no-render` run as final when the user asked for a finished result; continue through render or explicitly report that render was intentionally skipped.
- Treat leftover `_part...` uploads and any older `_copyright_silence` leftovers as intermediate Suno Library artifacts to remove after the run.
- Verified end-to-end on macOS with `VIRAL AiNA THE END Remix.mp3`: split/upload produced 61 clips, Studio save succeeded using the blank-state fallback, render completed, and the final MP3 downloaded locally.
