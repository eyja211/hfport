# Contributing

## Scope

Contributions should keep hfport focused on portable Hero Fighter-compatible
data layouts, deterministic binary transformations, and verifiable output.
Avoid unrelated refactors and do not change original input files in place.

## Do Not Upload Game Content

Do not commit or attach any of the following:

- Game EXEs, SWFs, installer files, or generated ported EXEs
- Extracted game source, textures, sounds, or other assets
- FFDec binaries or JAR files
- Personal paths, saved configurations, or reports produced from game files

For a compatibility report, describe the build and its structural difference in
your own words. Redact proprietary content and include only a minimal synthetic
fixture when one is necessary and you have the right to share it.

## Development Checks

Run these checks before opening a pull request:

```powershell
python -m compileall hfport
python -m hfport.cli --help
```

The scripts under `hfport/tests/` are integration checks. They require local,
legitimately obtained game inputs and FFDec, so they are not expected to run
from a fresh public clone.

## Pull Requests

Keep each pull request narrowly scoped. Explain the target data-layout
assumption, the behavior changed, and how you validated it. Changes that affect
an output build should preserve the existing rule: generate a new explicit
output path and never overwrite the source or target input.
