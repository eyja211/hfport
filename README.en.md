# hfport

[**简体中文**](README.md) | **English**

**Hero Fighter Character Porter** is a source-only tool for moving selectable
characters between compatible Hero Fighter game builds and mods.

It analyzes a character's data dependencies, resolves name collisions, patches
the target build's registration points, and writes a new output EXE without
overwriting either input file.

> This repository deliberately does not contain Hero Fighter executables, SWF
> files, game assets, FFDec, or generated ports.

## What It Ports

- Character SPT data and LMI textures
- Referenced attacks and Pt templates from `globalDat`
- Isolated copies of referenced item action groups, projectiles, and textures
- Referenced sound assets when the target does not already contain them
- Character-list and VS-selection registration
- Experience binding to an existing target character

Every run gets a short source-derived namespace. If that namespace is already
present in the target, hfport automatically allocates the next numbered variant.
Item groups are always copied to new indices even when source and target use the
same original `int1`; a ported character therefore cannot silently use the
target mod's edited version of that item.

## Compatibility

hfport is intended for the Hero Fighter family and compatible Flash-based mods;
it is not restricted to one named mod or a single version label.

The current implementation expects a Windows Flash projector containing an
embedded SWF and the usual Hero Fighter-style data and registration layout:

- `Data.Global_*` binary assets, including `globalDat`, `itemSpt`, and `itemLmi`
- `Data.BgInfoFile` data loading
- `Data.Global` character registration and experience initialization
- `Web_misc.SelectCharPanel` character selection
- `Page.CharList` character-list paging

Different releases and heavily modified mods can change those layouts. Use a
copy of the target, run the built-in structural validation, and complete the
manual in-game checklist before treating a port as usable. The tool should not
be read as a guarantee of compatibility with every Hero Fighter release or mod.

## Requirements

- Windows
- Python 3.8 or newer; no Python packages beyond the standard library
- Java
- [JPEXS Free Flash Decompiler (FFDec)](https://github.com/jindrapetrik/jpexs-decompiler)
  for extracting the small registration-code surface used by the tool

Place `ffdec.jar` at `FFDec/ffdec.jar`, or provide its path in a configuration
file. Keep FFDec and game files outside Git; the supplied `.gitignore` handles
the common local layout.

## Quick Start

1. Clone this repository.
2. Put `FFDec/ffdec.jar` beside the source, or prepare an absolute FFDec path.
3. Keep copies of your source and target game EXEs in a local working folder.
4. Run either `移植工具.bat` or:

   ```powershell
   python -m hfport.cli
   ```

5. Choose the source build, target build, and characters to import. A build that
   was already extended by hfport can be used as either source or target.
6. Review the static validation output, then test the generated EXE in-game.

The tool writes a distinct output file under `output/` by default. It does not
overwrite either selected input EXE.

## Non-Interactive Use

Copy `examples/port-config.example.json`, replace every placeholder with your
own local paths, then run:

```powershell
python -m hfport.cli --config path\to\port-config.json
```

To list selectable source characters without creating an output:

```powershell
python -m hfport.cli --list "C:\path\to\source-build.exe"
```

## Validation

After a build, hfport validates the generated projector/SWF structure, ABC
round-trips, generated assets, major data references, and registration strings.
Passing that validation is necessary but not sufficient: a final in-game check
should cover loading, character list, VS selection, battle entry, animations,
attacks, projectile/item behavior, textures, sounds, and experience binding.

The historical integration scripts in `hfport/tests/` require local game and
FFDec files that are intentionally excluded from this repository. A source-only
sanity check is available everywhere:

```powershell
python -m compileall hfport
python -m hfport.cli --help
```

Registry exports are cached by the exact SHA-256 of each input EXE. Switching
between mods cannot reuse another build's stale `pow`, binding, or selection data.

## Repository Layout

```text
hfport/                 Core parser, porter, registry patcher, and CLI
hfport/tests/           Integration scripts for locally supplied game files
examples/               Redacted configuration examples
移植工具.bat             Windows launcher
work/                   Generated scratch data (ignored)
output/                 Generated ports and configs (ignored)
```

## Safety Notes

- Work from copies of the source and target files.
- Never publish game executables, SWFs, extracted assets, or generated ports
  unless you have the rights to distribute them.
- Both players need the same generated game build for multiplayer use.
- Engine-level gameplay changes in a source mod are outside this tool's
  data-porting scope and will continue to use the target build's engine logic.

## Contributing

Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening an issue or pull
request. In particular, do not attach proprietary game files or extracted game
assets to the repository, issues, or pull requests.

## License

The source code in this repository is available under the [MIT License](LICENSE).
Hero Fighter, game builds, game assets, and third-party tools remain the property
of their respective owners. This project is not affiliated with or endorsed by
those owners.
