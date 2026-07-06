# Fengling Plugin

This plugin packages the Mac-native Fengling CLI source and the Codex skill for
the migrated Suno recut/upload/render workflow.

## Contents

- `skills/fengling/SKILL.md` - Codex operating instructions for Fengling.
- `scripts/fengling-cli/` - CLI source package. Install locally from this folder
  with `make install-local`.

## First Run

Use the skill's first-run setup section before any live workflow. It checks the
local CLI, `~/fengling-studio`, Python dependencies, `ffmpeg`, Chrome, and
OpenCLI. External tools should be installed only after user approval.

## Runtime Notes

- The workflow reuses the OpenCLI `fengling-suno` browser session for Suno login.
- The final rendered song should be kept.
- Uploaded `_part...` clips and older `_copyright_silence` leftovers are
  intermediate Suno Library artifacts and should be cleaned after a successful
  run.
