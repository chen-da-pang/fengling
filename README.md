# Fengling Plugin

This plugin packages the Mac-native Fengling CLI source and the Codex skill for
the migrated Suno recut/upload/render workflow.

## Contents

- `skills/fengling/SKILL.md` - Codex operating instructions for Fengling.
- `scripts/fengling-cli/` - CLI source package.
- `scripts/fengling-backend/` - the bundled local backend needed by the CLI.
- `scripts/install-fengling-local.sh` - installs the bundled backend into
  `~/fengling-studio`, installs the local CLI wrapper, and writes the CLI config.

## First Run

Use the skill's first-run setup section before any live workflow. It checks the
local CLI, `~/fengling-studio`, Python dependencies, `ffmpeg`, Chrome, and
OpenCLI. External tools should be installed only after user approval.

After the user approves local setup, run:

```bash
./scripts/install-fengling-local.sh
fengling --json doctor
```

## Runtime Notes

- The workflow reuses the OpenCLI `fengling-suno` browser session for Suno login.
- The final rendered song should be kept.
- Uploaded `_part...` clips and older `_copyright_silence` leftovers are
  intermediate Suno Library artifacts and should be cleaned after a successful
  run.
