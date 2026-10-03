# altium-helper

Lets Claude read Penn Electric Racing's Altium 365 board designs and their history, read-only, on Linux, Windows and
macOS, without Altium Designer. `README.md` covers what it does, how it works and how to install it.
`docs/research.md` has the verified facts behind the design.

## Status (2026-10-03)
Built and tested on public boards and on synthetic history. On 2026-10-03 it ran against the team's own Altium 365
workspace: `altium-helper login` (Nexar browser sign-in) and git with the user's Altium email and password (route B)
both work, and all six tools read a real board.

Possible next steps:
- team rollout: each member needs their own account, license, Nexar app and git sign-in, and `exclude` is per user;
- route A (Altium desktop sign-in, once Altium issues a client ID);
- a standalone firmware pin checker, if the skill's recipe isn't enough.

## Rules
- **Read-only toward Altium 365.**
  - `api.graphql` refuses any document containing `mutation` or `subscription`. Never weaken that.
  - Never push to an Altium 365 repository. Board clones get an invalid push URL and a `pre-push` hook that
    always fails. Only clone, fetch, fast-forward merge, worktree and read commands are allowed.
- **Credentials go only to `*.altium.com`** (`boards._credentials_for`). They're passed as a URL-scoped header in
  the environment: never on a command line, in a URL or in git config.
- **Never commit board data or secrets.** That covers Altium files, `*.netlist.json`, zips, `designs/` and
  credentials. `.gitignore` and `hooks/pre-commit` enforce it. Test designs are built at runtime (`unformat.build`)
  or downloaded (`scripts/fetch_test_designs.py`); never add design files to the repo.
- **Everything an agent reads from a design goes to the AI provider.** Some parts may be under sponsor NDA. Boards
  in `exclude` must never be fetched by any route (`Board.ensure_allowed`).
- **Cross-platform.**
  - Use `pathlib`, and run subprocesses with argument lists, never `shell=True`.
  - Text files use LF line endings (`.gitattributes`).
  - CI runs on Ubuntu and Windows.

## Layout (`src/altium_helper/`)
- **Interfaces:**
  - `cli`: the `altium-helper` command.
  - `mcp_server`: the six read-only tools Claude calls. It falls back to the API when git fails.
  - `setup_cmd`: finds `claude` (on PATH or bundled in an editor extension) and registers the servers and the
    skill.
- **Boards and history:**
  - `boards`: board registry, blobless clones, sparse worktrees, cached per-revision analysis.
  - `git`: runs git, handles credentials, adds the read-only guards.
  - `history`: history and changes over a range.
  - `diff`: netlist comparison (moves, swaps, rotations, renames, parts).
  - `timeparse`: "yesterday" and friends.
- **Reading designs:**
  - `netlist`: installs (npm lockfile) and runs universal-netlist on the Node from `nodejs-wheel-binaries`.
  - `checks` and `pcbdoc`: missing documents, and schematic vs PCB (`.PcbDoc` pad nets).
  - `unformat`: writes Universal Netlist files, including universal-netlist's JavaScript-order content hash.
- **Altium 365 access:**
  - `api`: the query-only GraphQL client.
  - `nexar`: browser sign-in (PKCE, localhost:3000).
  - `cloud`: board discovery, comments, revisions, PCB snapshot.
- **Settings:** `config` covers paths and settings (`config.json`) and credentials (`credentials.json`, 0600).
- **Data:** `data/universal-netlist/` (the version pin) and `data/skill/SKILL.md`.

User data lives outside the repo:
- board copies and caches in `~/.local/share/altium-helper` (Windows: `%LOCALAPPDATA%`);
- settings in `~/.config/altium-helper` (Windows: `%APPDATA%`).

## Development
```bash
uv sync                           # .venv with dev dependencies
uv run pytest                     # all tests; -m "not network" skips npm and board downloads
uv run black . && uv run isort .
uv tool install --editable . && altium-helper setup    # use your working copy for real
```
Tests point every data and config folder into `.pytest_cache` (see `tests/conftest.py`). The synthetic DAQ board
repository fixture is there too.

## Git conventions
- Keep `main` working. Make each change on a short-lived branch (`feat/...`, `fix/...`, `docs/...`) and merge it as
  one squashed commit.
- Commit subjects are imperative and capitalized, at most 72 characters, with no final period. The body explains
  why.
- Version pins live in git: `uv.lock`, and `src/altium_helper/data/universal-netlist/package-lock.json`. Bump
  universal-netlist in its own change and keep `tests/test_accuracy.py` passing.

## Facts that are easy to get wrong
- **universal-netlist's trace tool** (`query_xnet_by_pin_name`):
  - takes pin numbers, not names;
  - traces through R, L, C and FB parts only;
  - ignores junction dots;
  - needs `design_variant` whenever the design defines variants.

  Its standalone binary updates itself on every start; we use the npm package instead.
- **Universal Netlist hash.** It's computed with JavaScript's `JSON.stringify`: integer-like keys come first, in
  numeric order. Unknown component fields are dropped before the check.
- **Nexar** has the latest design, releases, a commit list and comments, but no past revisions and no schematic
  nets. `desWorkspaces` is deprecated; use `desWorkspaceInfos`.
- **MCP Python SDK 2.x** renamed `FastMCP` to `MCPServer` (`mcp.server.mcpserver`). It runs plain (non-async) tools
  on worker threads, so parallel tool calls really run at once: anything touching a board's files goes through
  `Board._lock`.
- **Altium 365 commits often have no message**, because Altium doesn't ask for one. Python's `str.strip()` treats
  the `\x1f` separator in our git formats as whitespace, so never strip before splitting on it.
- **Nexar comment threads:** `status` 0 means resolved, 1 means active.
- **Multi-channel `.PcbDoc`** stores logical designators. The board designator is `SOURCEDESIGNATOR`, then `_`,
  then the room name from `SOURCEHIERARCHICALPATH`.

## Firmware monorepo (Penn-Electric-Racing, e.g. `~/github/per/Penn-Electric-Racing`)
- **STM32 pin definitions:** `embedded/boards/*/*Pins.hpp`, e.g. `const Pin framMosi = PC12;`. Known mismatch:
  `BMSPins.hpp` `canRx`/`canTx` ("Swapped compared to schematic").
- **Ludwig (CM4):**
  - `embedded/boards/ludwig/configs/firmware/config.txt`:
    - two MCP251xFD CAN controllers on SPI1;
    - chip selects on GPIO18 and GPIO16, interrupts on GPIO7 and GPIO12;
    - SPI1 pins: MOSI GPIO20, MISO GPIO19, SCLK GPIO21.
  - `LudwigPins.hpp`: `encA` and `encB` are marked "NOT ON SCHEMATIC".
