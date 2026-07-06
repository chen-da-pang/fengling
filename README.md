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

Use the skill's first-run setup section before any live workflow. The plugin
already includes the Fengling backend payload, so a missing `~/fengling-studio`
means the bundled installer has not been run yet. It does not mean the user must
find an old app directory.

After the user approves local Fengling setup, run from the plugin root:

```bash
./scripts/install-fengling-local.sh
fengling --json doctor
```

Then install any remaining external runtime dependencies only after user
approval. Those external pieces include Python packages, `ffmpeg`, Chrome, and
OpenCLI.

## Runtime Notes

- The workflow reuses the OpenCLI `fengling-suno` browser session for Suno login.
- The final rendered song should be kept.
- Uploaded `_part...` clips and older `_copyright_silence` leftovers are
  intermediate Suno Library artifacts and should be cleaned after a successful
  run.
