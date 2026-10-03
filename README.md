# altium-helper

Lets Claude read Penn Electric Racing's board designs in Altium 365, and follow how they change. It runs on Linux
(including WSL) and Windows, read-only, with no Altium Designer and no manual downloads.

Ask Claude "did my team change the flipped pins on the DAQ board yesterday?". The tool fetches the DAQ project's new
revisions from Altium 365 by itself and compares them. Claude then answers with who changed what and when, which
pins moved or swapped, and whether the PCB was updated to match.

> **Status (2026-10-03):** built and tested, including on our own Altium 365 workspace: `altium-helper login` and
> git with an Altium email and password both work. See [What's verified](#whats-verified).

## What it can do

- **Fetch boards itself.** Git copies of each Altium 365 project, updated whenever Claude asks about a board. A
  first copy holds the latest revision; older history downloads when a question needs it. Nobody downloads files
  by hand.
- **Answer "what changed" questions.** For any time range or pair of revisions it reports:
  - each commit's author, time and message;
  - pins that moved to another net;
  - pins that **swapped** nets, or passed them around in a circle (**rotated**);
  - nets renamed, added or removed;
  - parts added, removed or changed;
  - whether the PCB file changed, and whether it still matches the schematic.
- **Answer connectivity questions** about the latest version or any past revision: where a pin or net goes, a part's
  pins, searches, traces through passives, and basic electrical rule checks.
- **Check firmware against the hardware.** Ask Claude to compare a `*Pins.hpp` file or Ludwig's `config.txt` with
  a board. The installed skill gives it the recipe, including the direction rules that catch a MOSI/MISO swap.
- **Read the team's Altium 365 comments** on a board.
- **Check the data itself.** At any revision it confirms the design documents are all present and the schematic
  matches the PCB file.
- **Stay read-only.** It never writes to Altium 365 (see [Read-only, secrets and privacy](#read-only-secrets-and-privacy)).

Not included yet:
- **A separate pin-checker program.** Claude does it with the skill's recipe.
- **Images of schematic sheets.**
- **Team rollout.**
- **Testing on macOS.** It should work there, since the same packages exist.

## How it works

```
                  Claude Code (VS Code extension or CLI; Linux, WSL or Windows)
                    │                                      │
                    ▼                                      ▼
     altium-helper (MCP server)                universal-netlist (MCP server)
     boards, history, changes, comments,       parts, nets, pins, traces, ERC
     checks                                    on the local copies
       │                     │                             ▲
       │ git clone/fetch     │ GraphQL queries only        │ reads
       ▼                     ▼                             │
  Altium 365 project     Nexar, or the Altium 365          │
  repositories           API: board list, git URLs,        │
  (every revision)       commit list, comments,            │
                         PCB fallback                      │
       └──────────► data folder: designs/<board>/ ─────────┘
                    git copy + a cached netlist for each revision looked at
```

An MCP server is a small program Claude Code starts in the background and calls as tools. There are three pieces:

- **altium-helper**, the Python package in this repo, and a command.
  - It signs in, lists the workspace's boards, and keeps a git copy of each board. A board's first copy holds just
    its latest revision, about 10-20 seconds to fetch. Older history downloads when a question needs it, only as
    far back as it needs.
  - It reads any past revision into a temporary folder holding just the project, works out its netlist with
    universal-netlist, and compares revisions.
  - Claude calls it through six read-only tools: `list_boards`, `get_board`, `board_history`, `board_changes`,
    `board_comments`, `check_board`.
- **[universal-netlist](https://github.com/IntelligentElectron/universal-netlist)** reads Altium projects without
  Altium and answers connectivity queries. `get_board` gives Claude the path to pass it. A past revision is handed
  over as universal-netlist's own `.netlist.json` file, so every revision is queried the same way.
- **The `altium-boards` skill** tells Claude how to use both servers together. It also covers the quirks listed
  under [Known limits](#known-limits) and the firmware pin-check recipe.

`board_history` reads Altium 365's own commit list (the same commits as git, with the files each one changed)
whenever the local copy doesn't hold that history yet, so listing commits never waits for a download. The list
also tells the other history tools how far back to fetch, in one request.

If git can't reach a board at all, `get_board` falls back to the current PCB from Altium's API: pads, nets and
symbol pin names, with no schematic wiring. The result says it's PCB-only.

## Getting into Altium 365

The tool needs git access to fetch design files and history, and API access for the board list, git URLs and
comments. Git is the only way to get the files and past revisions: Altium's APIs have neither.

| Route | How | What it needs | Status |
|---|---|---|---|
| **B. Git with your Altium email and password** | `altium-helper login` offers it, and tests it at once | An Altium account with a password (not Google sign-in) | Works with our workspace (2026-10-03) |
| **C. An Altium 365 API token** | `altium-helper login --token` (used for git too) | A workspace admin creates it in Admin → Developer, if our workspace has that page | Documented by Altium |
| **A. Altium's desktop sign-in** | Not built yet | Altium registers this tool as an app. There's no self-service ([altium-auth](https://github.com/AltiumDeveloper/altium-auth)) | The long-term route for the team |
| **D. Nexar only** | What happens if no git route works | A Nexar app plus an Altium Designer license | Documented. PCB data and the commit list only |

`altium-helper login` does the rest:
1. Signs in to Nexar in your browser.
2. Picks the workspace.
3. Lists every board with its git URL.
4. Offers route B, and tests it on one board. A refused password isn't kept.

## Tools it uses

| Tool | Used for | Version and license |
|---|---|---|
| [universal-netlist](https://github.com/IntelligentElectron/universal-netlist) | Reading Altium projects; connectivity queries | 1.12.0, pinned by a committed npm lockfile. Apache-2.0 |
| [nodejs-wheel-binaries](https://github.com/njzjz/nodejs-wheel) | The Node.js that runs universal-netlist, installed by uv like any Python package, so nobody installs Node | Node 24. MIT |
| Git | Board copies and history (shallow clones, sparse worktrees) | 2.36 or newer |
| [Nexar API](https://nexar.com) and the Altium 365 API | Sign-in, board list, git URLs, commit list, comments, PCB fallback (queries only) | — |
| [MCP Python SDK](https://pypi.org/project/mcp/) | altium-helper's MCP server | 2.x. MIT |
| [olefile](https://github.com/decalage2/olefile) | Reading `.PcbDoc` files for the schematic-vs-PCB check | 0.47. BSD-2-Clause |
| Python 3.10+ and [uv](https://docs.astral.sh/uv/) | Running and installing altium-helper. uv fetches Python if needed | — |

Considered and not used:
- **The standalone universal-netlist download** replaces itself with every new release, so it can't be pinned.
- **The Altium 365 API's own schematic netlist** is closed beta.
- **MCP servers built on Altium Designer** need Altium Designer running on Windows.
- **Scripting the "Download Sources" button** breaks when the page changes, and isn't an interface Altium supports.
- **Nexar's cloud export job** is a mutation, so it would break read-only.

Details are in [docs/research.md](docs/research.md).

## Install

You need Git and uv. Nothing else: uv brings Python, and Python brings Node.

**Arch Linux:**
```bash
sudo pacman -S --needed git uv
```

**Ubuntu or WSL:**
```bash
sudo apt install -y git && curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell):**
```powershell
winget install --id Git.Git -e; winget install --id astral-sh.uv -e
```

Then, in a new terminal:
```bash
git clone git@github.com:laytcai/altium-helper.git     # private repo: SSH, or HTTPS after `gh auth login`
cd altium-helper
uv tool install --editable .     # puts `altium-helper` on your PATH (run `uv tool update-shell` if it isn't)
altium-helper setup              # installs universal-netlist and registers everything with Claude
altium-helper login              # once: sign in to Altium 365 and find the boards
```

Restart Claude afterwards so it loads the tools.

**What `setup` does.** It's safe to run again, and it's how you update.
1. Installs universal-netlist 1.12.0 with `npm ci` from the committed lockfile.
2. Registers the `altium-helper` and `universal-netlist` MCP servers with Claude Code for your user. It uses
   `claude` from PATH or the copy bundled with the VS Code, VS Code OSS, VS Code Server or Cursor extension. It
   registers them with Codex too, if Codex is installed.
3. Installs the skill into `~/.claude/skills/altium-boards/`.
4. Turns on this repo's git hooks.

Use `--no-register` to print the registration commands instead of running them.

**The Nexar app, needed before `login`:**
1. Sign in at [portal.nexar.com](https://portal.nexar.com) with your Altium 365 account. If it asks you to create
   an organization, do so.
2. Open the "Evaluation App" new accounts get, or create an app with the Design scope (Supply isn't needed). Copy
   its client ID and secret; `login` asks for them. You can also set `NEXAR_CLIENT_ID` and `NEXAR_CLIENT_SECRET`.
   Design queries are free for an account with an Altium Designer license in a workspace.
3. The sign-in comes back to `http://localhost:3000/login`. If Nexar reports a redirect error, add that address to
   the app's allowed redirect URLs.

## Use

**Ask Claude.** Open Claude anywhere, for example in the firmware repo, and ask normally:
- "Did my team change the flipped pins on the DAQ board yesterday?"
- "What changed on Ludwig since last Friday that affects firmware?"
- "On Ludwig, which CM4 GPIOs go to the CAN controllers' SDI, SDO, SCK, CS and INT? Compare with
  `embedded/boards/ludwig/configs/firmware/config.txt`."
- "Check `BMSPins.hpp` against the BMS board."
- "Any Altium comments on the PDU about the fuse footprint?"

**The command line** covers the same things, for checking by hand:
```bash
altium-helper boards [--refresh]               # boards Altium 365 lists
altium-helper sync ludwig                      # fetch new revisions now (Claude does this itself)
altium-helper history ludwig --since yesterday
altium-helper changes ludwig --since "3 days ago"      # or --from <commit> --to <commit>
altium-helper check ludwig [--rev <commit>]    # design documents present? schematic matches PCB?
altium-helper comments ludwig
altium-helper add-board <key> <git-url> [--project path/Board.PrjPcb]   # a board no API lists
```
Most commands also take `--json`.

**Where things live** (outside the repo):

| | Linux / WSL | Windows |
|---|---|---|
| Board copies, netlist cache, universal-netlist | `~/.local/share/altium-helper/` | `%LOCALAPPDATA%\altium-helper\` |
| Settings (`config.json`) and credentials (`credentials.json`, private to you) | `~/.config/altium-helper/` | `%APPDATA%\altium-helper\` |

`ALTIUM_HELPER_DESIGNS`, `ALTIUM_HELPER_DATA` and `ALTIUM_HELPER_CONFIG` move these. `config.json` also holds
`exclude`, a list of boards that must never be fetched, by name, key or project id. When several projects share a
name, use the project id (`altium-helper boards` shows it): a key can move to another project when Altium lists
them in a different order.

**Updating:**
```bash
git pull && altium-helper setup
```
Then restart every open Claude Code session, or reconnect altium-helper with `/mcp`: a running server keeps the code
it started with.

## Git plan and conventions

### This repository
- **Hosting.** Private, on GitHub at `laytcai/altium-helper`. Transfer it to the Penn-Electric-Racing organization
  if the team adopts it; a transfer keeps the history and redirects the old URL.
- **Branches.** `main` always works. Each change is a short-lived branch (`feat/...`, `fix/...`, `docs/...`),
  merged as one squashed commit, as in the firmware repo. Use pull requests once others contribute.
- **Commit messages.** Imperative, capitalized subject, at most 72 characters, no final period. The body explains
  why. Commits written with Claude carry a `Co-Authored-By: Claude` trailer.
- **CI.** GitHub Actions runs black, isort and the tests on Ubuntu and Windows for every push and pull request.
- **Version pins live in git.**
  - Python: `uv.lock`.
  - universal-netlist: `src/altium_helper/data/universal-netlist/package-lock.json`.
  - Updating universal-netlist is its own change, and the accuracy tests must still pass.
- **Never committed:**
  - board files and anything made from them (`*.PrjPcb`, `*.SchDoc`, `*.PcbDoc`, `*.netlist.json`, zips,
    `designs/`, `test-designs/`);
  - secrets.

  `.gitignore` covers these, and `hooks/pre-commit` blocks them even when forced (`setup` enables it).
- **Formatting.** LF line endings everywhere (`.gitattributes`). black and isort with default settings, like the
  firmware repo.

### Board repositories in Altium 365
- **One repository per project.** Each Altium 365 project is a git repository, and the API supplies its URL.
- **Cloning.** A board is cloned the first time a tool needs its files, latest revision only (`--depth=1`).
  Altium's git server has no partial clone, and most of a board's history is old versions of its `.PcbDoc`, so a
  full clone took 43-62 s (up to 5 minutes on a slow day) where the latest revision takes 7-20 s.
  - History tools fetch older commits when they need them, only as far back as they need (`--deepen`,
    `--shallow-since`), and never shorten what's already there. The server won't send a commit by its id, so an
    old revision named only by its id is fetched in growing steps, at worst with the whole history
    (`--unshallow`).
  - Later requests fetch only new commits, at most every 5 minutes.
  - A copy whose first clone was interrupted is cloned again.
- **Past revisions** are read from a temporary worktree holding just the project's folder. The netlist and the PCB
  check of each revision are cached by commit.
- **Read-only.** Clones get an invalid push URL and a `pre-push` hook that always fails. The tool runs only clone,
  fetch, fast-forward and read commands. If a copy has local edits, sync stops instead of overwriting them.
- **Credentials** reach git only through a URL-scoped header in the environment, and only for `*.altium.com`
  hosts. They never go in a URL or git's config.

## Read-only, secrets and privacy

- **Nothing writes to Altium 365.** The GraphQL client refuses any document containing a mutation or
  subscription; Nexar and Altium 365 tokens *can* write. Git pushes are blocked twice over.
- **Credentials live in `credentials.json`,** readable only by you: Nexar tokens and, for route B, your Altium
  password. They never go in the repo.
- **What Claude reads goes to Anthropic.** Some parts may be under sponsor NDA. Put any board that mustn't be sent
  to an AI provider in `exclude`, and it's never fetched, by any route.
- **universal-netlist's usage log is turned off.**

## Known limits

- **Git needs an Altium password** (route B). An account that signs in with Google has none, so it needs route C
  or gets the PCB fallback.
- **History only covers what's been saved to Altium 365.** Unsaved work in someone's Altium Designer is invisible.
- **universal-netlist's trace tool** has quirks. The skill warns Claude about each one, and `check` compares
  against the PCB.
  - It takes pin *numbers* (`U3.12`).
  - It only passes through R, L, C and FB parts.
  - It ignores junction dots.
  - It treats off-sheet connectors as global.
  - Boards with variants need a variant name.
- **The PCB fallback** has no schematic wiring and no per-commit diffs. Pin names come only from
  workspace-library parts.

## What's verified

On 2026-10-03, against our Altium 365 workspace (Ubuntu 24.04):

- **Sign-in.** `altium-helper login` signed in through Nexar, found the workspace and listed every project with its
  git URL.
- **Git with an Altium email and password** (route B) cloned a board with its full history.
- **All six tools** worked on that board through the MCP server, two calls at a time. Getting there found three
  bugs, now fixed:
  - commits with no message, which are common in Altium, stopped every sync;
  - comment threads showed resolved as open;
  - two calls on a board nobody had fetched yet collided.
- **Automated tests.** 73 pass, including new ones for each of those bugs.

On 2026-09-30, in WSL (Ubuntu 26.04):

- **Accuracy.** Checked against each board's own PCB pad data, with the pinned universal-netlist and our PCB reader:

  | Board | Nets matched | Net names |
  |---|---|---|
  | ST NUCLEO-144 | 287/287 | identical |
  | UBC mainboard | 227/227 | identical |
  | UBC breakout (multi-channel sheets and harnesses) | 79/79 | identical |

- **History on real commits** (the public UBC repo):
  - "Reconfigured MCU pinout" came out as one pin swap (PF9 ↔ PE5), one 3-pin rotation and six moves.
  - "Replaced 3V3 power select diodes with jumpers" came out as D2 removed and 0 Ω R104/R105 added, with the PCB
    still matching.
- **A real Claude session** answered on its own, using only these tools:
  - It explained that pinout commit, including that the PCB wasn't updated in that commit.
  - It traced a board's SPI1 pins through series resistors to their far-end parts.
- **Registration.** `setup` registered both servers with Claude Code in WSL, and `claude mcp list` shows both
  connected.
- **Automated tests.** 69 tests pass, covering:
  - the diff engine;
  - boards and history on a synthetic repository;
  - the MCP server over stdio;
  - a Nexar sign-in against a fake browser;
  - the API fallback;
  - the pre-commit hook.

  CI runs them on Ubuntu and Windows.

## Changes from the earlier plan

| Earlier plan (CLAUDE.md, then the plan README) | Now | Why |
|---|---|---|
| A team tool first | A personal tool first, on Linux and Windows | Your decision |
| Fetch with an admin token; manual zips as the fallback | Fully automatic. Routes B and C are built in, A comes later, and D is the fallback | No manual downloads |
| Nexar as optional extras | Nexar for sign-in, board list, git URLs, the commit list, comments and the PCB fallback | It works with a member's own login |
| Node installed separately | Node comes from a Python package | Git and uv are the only prerequisites |
| Board data inside the repo | In the user data folder | A clean repo, and one install serves every project |
| A firmware pin-checker program | The skill's recipe | Keeps the tool small; a real session showed Claude follows it |
| — | New: pin rotations, the PCB fallback, comments, the exclude list | Found while building |
