# altium-helper

Lets Claude read Penn Electric Racing's Altium 365 board designs and their history, read-only, on Linux, Windows and
macOS, without Altium Designer. What the tool does, how it works and how to install it are in `README.md`.
Verified facts and sources are in `docs/research.md`.

## Rules
- **Read-only toward Altium 365.**
  - GraphQL: send queries only, never mutations. The API client refuses any document that contains `mutation`.
  - Git: never push to an Altium 365 repository. Board clones get an invalid push URL and a `pre-push` hook that
    always fails. Only clone, fetch, worktree and read commands are allowed.
- **Never commit board data or secrets.** That covers Altium files, `*.netlist.json`, zips, `designs/`, tokens and
  credentials. `.gitignore` and `hooks/pre-commit` enforce it. Test fixtures are generated at runtime; never add real
  or public design files to the repo.
- **Everything an agent reads from a design goes to the AI provider.** Some parts may be under sponsor NDA.
- **Cross-platform.**
  - Use `pathlib` everywhere and no shell-only tricks.
  - Run subprocesses with argument lists, never `shell=True`.
  - Text files use LF line endings (see `.gitattributes`).

## Layout
- `src/altium_helper/`: the package.
  - `cli`: the `altium-helper` command.
  - `mcp_server`: the tools Claude calls.
  - `repo`, `history` and `diff`: git and revision comparison.
  - `pcbdoc`: pad-to-net reader for `.PcbDoc` files.
  - `netlist`: wrapper around universal-netlist.
  - `api` and `nexar`: GraphQL clients.
  - `config`: paths and settings.
  - `setup_cmd`: registration with Claude.
- `src/altium_helper/data/`: the pinned universal-netlist (`package.json` and `package-lock.json`) and the skill
  template.
- `scripts/`: developer helpers, such as downloading the public test boards.
- `tests/`: pytest. Tests marked `network` download public boards.
- `hooks/`: git hooks for this repository.

User data lives outside the repo:
- Board clones and caches go in the platform data dir: `~/.local/share/altium-helper` on Linux,
  `%LOCALAPPDATA%\altium-helper` on Windows.
- Settings and credentials go in the config dir: `~/.config/altium-helper` on Linux, `%APPDATA%\altium-helper` on
  Windows.

## Development
```bash
uv sync                      # create .venv with dev dependencies
uv run pytest                # tests; -m "not network" skips downloads
uv run black . && uv run isort .
uv run altium-helper --help
```

## Git conventions
- Keep `main` working. Make each change on a short-lived branch (`feat/...`, `fix/...`, `docs/...`), open a pull
  request, and squash-merge it.
- Commit subjects are imperative and capitalized, at most 72 characters, with no final period. The body explains why
  when that isn't obvious.
- Pin versions in git: `uv.lock` for Python, and `src/altium_helper/data/universal-netlist/package-lock.json` for
  universal-netlist. Bump universal-netlist in its own PR and re-run the network tests.
- Enable the hooks once per clone with `git config core.hooksPath hooks` (`altium-helper setup` does this).

## Facts that are easy to get wrong
- **universal-netlist's trace tool** (`query_xnet_by_pin_name`):
  - takes pin numbers (`U14.35`), not pin names;
  - only walks through R, L, C and FB parts;
  - ignores junction dots;
  - treats off-sheet connectors as global;
  - needs `design_variant` whenever the design defines variants.
- **universal-netlist's standalone binary** updates itself on every start. That's why we install the npm package
  instead.
- **Nexar** only has the latest design, releases, a commit list and comments. It has no past revisions and no
  schematic nets. Git is the only source of files and history.
- **Multi-channel boards.** A `.PcbDoc` stores the logical designator (`SOURCEDESIGNATOR`, e.g. `J4` five times).
  The physical designator is that value plus `_` plus the last element of `SOURCEHIERARCHICALPATH` (e.g.
  `J4_U_bob_0`).

## Firmware monorepo (Penn-Electric-Racing, locally `/home/lycai/Penn-Electric-Racing`)
- **STM32 pin definitions:** `embedded/boards/*/*Pins.hpp`, e.g. `const Pin framMosi = PC12;`. There are about 180
  active pins across 6 STM32 boards. Known mismatches: `BMSPins.hpp` `canRx`/`canTx` ("Swapped compared to
  schematic").
- **Ludwig (Raspberry Pi CM4):**
  - `embedded/boards/ludwig/configs/firmware/config.txt`:
    - two MCP251xFD CAN controllers on SPI1;
    - chip selects on GPIO18 and GPIO16, interrupts on GPIO7 and GPIO12;
    - SPI1 pins: MOSI GPIO20, MISO GPIO19, SCLK GPIO21.
  - `LudwigPins.hpp`: `encA` and `encB` are marked "NOT ON SCHEMATIC".
