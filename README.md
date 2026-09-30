# altium-helper

Lets Claude read Penn Electric Racing's board designs in Altium 365, and follow how they change. It runs on
Windows and Linux, and needs neither Altium Designer nor anyone downloading files by hand.

> **Status: plan only. Nothing is built yet.** This README describes the planned tool so it can be reviewed before
> any code is written. The evidence behind the decisions is in [docs/research.md](docs/research.md). Some of that
> file is out of date; [Known limits and evidence](#known-limits-and-evidence) lists what was re-checked on
> 2026-09-30.

**Why.** Firmware bugs from wiring mismatches are easy to catch if whoever writes the firmware can look at the real
board and see what changed. One example is a CAN-to-SPI link with MOSI and MISO swapped. With this tool you ask
Claude "did my team change the flipped pins on the DAQ board yesterday?". The tool fetches the DAQ project's recent
revisions from Altium 365 by itself and compares them. Claude then answers with who changed what and when, which
pins moved, and whether the PCB was updated to match.

**Scope.** This is a personal tool for now: one user, on Windows and on Linux (including WSL). Team use may come
later (see [Changes from the earlier plan](#changes-from-the-earlier-plan)).

## What it can do

1. **Fetch everything itself.** When Claude needs a board, the tool downloads it straight from Altium 365, along
   with its full revision history. After that it only downloads new changes. Nobody downloads files by hand.
2. **Answer "what changed" questions.** For any time range or any two revisions it reports:
   - who saved what to Altium 365, when, and with what commit message;
   - which pins moved to a different net;
   - which nets were added, removed or renamed;
   - which parts were added, removed or changed (part number, value, fitted or not);
   - whether the PCB file changed too, and whether it matches the new schematic.
3. **Answer connectivity questions** about the latest version or any past revision. Claude can find where a pin or
   net goes, list a part's pins, search parts and nets, trace a net through resistors, capacitors, inductors and
   ferrites, and run basic electrical rule checks.
4. **Check firmware against the hardware.** Ask Claude to compare a `*Pins.hpp` file, or Ludwig's `config.txt`,
   with the board. The installed skill tells it how: MCU pin name → pin number → net → far-end pins, plus direction
   rules such as "MOSI must land on SDI, SI or DIN".
5. **Read the team's comments** on a board in Altium 365, for example a thread about the flipped pins.
6. **Check the data itself.** At any revision it checks whether the schematic's connections match the PCB file.
7. **Stay read-only.** Nothing ever writes to Altium 365 (see [Read-only, secrets and privacy](#read-only-secrets-and-privacy)).

Not included yet, listed here so nothing is silently missing:

- **A separate pin-checker program.** Claude does the comparison for now. If its answers aren't reliable enough, we
  write one.
- **Images of schematic sheets.**
- **Team setup:** a shared install, and each teammate's sign-in.
- **Testing on macOS.** The same steps should work there.

## How it works

```
                   Claude Code (VS Code extension, on Windows or WSL)
                     │                                        │
                     ▼                                        ▼
      altium-helper (MCP server)                  universal-netlist (MCP server)
      boards, sync, history, diff,                parts, nets, pins, traces, ERC
      comments, checks                            on the local copies
        │                      │                              ▲
        │ git clone / fetch    │ GraphQL queries              │ reads
        ▼                      ▼                              │
   Altium 365 project      Altium's API: Nexar, or            │
   repositories            Altium 365 directly.               │
   (every revision)        Board list, git URLs,              │
        │                  comments, live PCB                 │
        └────────────► designs/<board>/ ──────────────────────┘
                       git copy of the project + a netlist for each revision looked at
```

An MCP server is a small program that Claude Code starts in the background and calls as tools. There are three
pieces:

- **`altium-helper`** is our Python program. It signs in to Altium, finds boards, clones and fetches each project
  with git, works out the netlist at any revision, compares revisions, reads comments and runs checks. Claude calls
  it as an MCP server. A small command line covers setup and sign-in.
- **universal-netlist** reads Altium design files and answers detailed connectivity questions. It reads both Altium
  projects and its own `.netlist.json` files. altium-helper saves one of those for each past revision that gets
  looked at, so Claude can query any revision the same way.
- **The `altium-boards` skill** is a short instruction file installed into Claude Code. It tells Claude how to use
  the two servers together, and the quirks listed under [Known limits](#known-limits-and-evidence).

**Example: "Did my team change the flipped pins on the DAQ board yesterday?"**

1. Claude asks altium-helper to find "DAQ", and it matches the project in Altium 365.
2. altium-helper syncs DAQ with git. The first time this is a full clone; after that it only fetches new
   revisions.
3. `history` lists yesterday's revisions: the time, author, commit message and changed files of each.
4. `diff` works out the netlist before and after (cached per revision) and compares them. For example:
   "`U3.12 (PB12)` moved from `CAN_TX` to `CAN_RX`; `U3.13` did the opposite; `DAQ.PcbDoc` unchanged, so the layout
   still has the old wiring".
5. `comments` finds any Altium 365 comment thread about those pins.
6. Claude answers yes or no, with who, when, which pins, and whether the PCB was updated. It cites the revisions.

## Getting into Altium 365 without manual downloads

The tool needs two kinds of access:

- **git**, to fetch each board's files and history;
- **Altium's GraphQL API**, for the board list, git URLs and comments.

Git is the only automatic way to get the files and past revisions. Nexar only offers the latest PCB and a list of
commits. It never gives past revisions or schematic wiring; this was checked against its live schema.

Which sign-in git accepts from you is the one open question. The routes, in order of preference:

| Route | How you sign in | What it gives | What it needs | Status |
|---|---|---|---|---|
| **A. Altium's own sign-in for desktop tools** | Once, in the browser, with your Altium account. The tool gets a token for the team workspace and renews it by itself. No secret is stored | git, plus Altium 365's own API (board list, git URLs, comments) | Altium must register altium-helper as an app and issue it a client ID. Per Altium, registration is "a guided process handled together with Altium — there is no self-service registration portal" | The method Altium documents ([altium-auth](https://github.com/AltiumDeveloper/altium-auth)). Altium's git page says git accepts any Altium 365 token issued for the workspace. To be confirmed |
| **B. Your Altium email and password** | git asks once and stores them in Git's credential manager | git only (board list and URLs come from Nexar) | Nothing, if Altium still accepts passwords. Impossible if your account has no password, e.g. if you sign in with Google | A public 2023 mirror job did this. Altium's current docs only mention tokens. Untested |
| **C. A token made by a workspace admin** | Pasted once into setup | git, plus Altium 365's own API | An admin, and our workspace must have Admin → Developer. The Altium 365 API is in closed beta, so that page may not exist | Documented |
| **D. Nexar only (fallback)** | Browser sign-in through a Nexar app | Board list, git URLs, the latest PCB, the commit list, comments. No files and no past revisions | A Nexar app, plus an Altium Designer license | Documented |

The plan:

- **Try B first.** It takes minutes.
- **Ask Altium for A at the same time.** It's the supported route, the team would need it later anyway, and it's
  the slowest step.
- **Use C** only if an admin can do it quickly.
- **If only D works,** the tool can still answer "what changed" at file level: who, when, the message, and which
  sheets changed. From the day we start, it can also answer at pin level for the PCB, by saving a snapshot whenever
  Altium 365 gets a new revision (a scheduled job). Schematic-level history needs A, B or C.

## Tools it uses

| Tool | Used for | Why this one | Version / license |
|---|---|---|---|
| [universal-netlist](https://github.com/IntelligentElectron/universal-netlist) | Reading Altium projects; Claude's connectivity tools | Reads `.PrjPcb`/`.SchDoc` on any OS without Altium, and is already an MCP server. It matched the PCB exactly on 3 public boards, including one with multi-channel sheets and harnesses | 1.12.0, pinned. Apache-2.0 |
| Node.js | Running universal-netlist | Installing from npm is the only way to pin universal-netlist's version: the standalone download checks GitHub every time it starts and replaces itself with any newer release | 20 or newer |
| Git | Fetching every board and its whole history; this repo | Altium 365 stores each project as a git repository | Any recent version |
| [altium-auth](https://github.com/AltiumDeveloper/altium-auth) | Signing in with your Altium account (route A) | Altium's official library for desktop sign-in. It doesn't need a local web server or a stored secret | 0.2.0 (preview). MIT |
| Nexar / Altium 365 GraphQL API | Board list, git URLs, comments, live PCB data | Altium's official APIs, and they use the same queries. Nexar works with your own login | Queries only |
| [MCP Python SDK](https://pypi.org/project/mcp/) | Running altium-helper as an MCP server | The official SDK | 2.x. MIT |
| Python + [uv](https://docs.astral.sh/uv/) | altium-helper itself | Python is what the team already uses for scripts. uv installs Python on Windows and pins dependencies in a lockfile | Python 3.10+ |
| [olefile](https://github.com/decalage2/olefile) | Reading `.PcbDoc` files for the schematic-vs-PCB check | Pure Python, and the prototype already uses it | 0.47. BSD-2-Clause |
| Claude Code | Asking the questions | You already use the VS Code extension on both OSes | — |

Considered and not used:

| Option | Why not |
|---|---|
| "Download Sources" zips from the Altium 365 web page | Someone has to download them by hand |
| A script that clicks "Download Sources" in a browser | It breaks whenever the page changes, struggles with Google-style sign-in, and isn't an interface Altium supports |
| Altium 365 API's own schematic netlist (`DesignData_Preview`) | Closed beta and experimental. Revisit if route A or C gives us API access |
| MCP servers built on Altium Designer (altium-mcp, eda-agent, altium-copilot) | They need Altium Designer running on Windows |
| KiCad's Altium import | Per the earlier research, released `kicad-cli` can't import `.SchDoc` and ignores Altium's net-scope rules |
| Our prototype parser (`research/prototype/`) | universal-netlist handles more (variants, multi-channel sheets, harnesses). The prototype stays as a cross-check |
| Nexar's cloud export job (runs an OutJob in Altium's cloud) | It's a mutation, so it breaks read-only. It's also undocumented, and one user reported it blocked |

## Install

You need Git, uv, Node.js 20+ and VS Code with Claude Code, plus an Altium 365 account in the team workspace.

**Windows (PowerShell):**

```powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
winget install --id OpenJS.NodeJS.LTS -e
# open a new terminal so PATH picks these up
git clone <repo-url> $HOME\altium-helper
cd $HOME\altium-helper
uv run altium-helper setup
altium-helper login
```

**Linux / WSL (Ubuntu 26.04):**

```bash
sudo apt install -y nodejs npm            # Node 22
curl -LsSf https://astral.sh/uv/install.sh | sh
# open a new shell so uv is on PATH
git clone <repo-url> ~/altium-helper
cd ~/altium-helper
uv run altium-helper setup
altium-helper login
```

Windows and WSL on the same PC count as two separate installs, because each Claude has its own settings and each
keeps its own copy of the boards. Run both steps in each. Both copies stay current by themselves.

**What `setup` does.** Running it again is safe, and it's also how you update.

1. Checks the Git, Node and uv versions.
2. Installs universal-netlist 1.12.0 into `tools/universal-netlist/` from the committed lockfile (`npm ci`).
3. Registers both MCP servers, `altium-helper` and `universal-netlist`, with Claude Code for your user. It uses the
   `claude` command if it's on your PATH; otherwise it uses the copy bundled with the VS Code extension.
4. Lets Claude call their read-only tools without asking every time. You can turn this off.
5. Installs the skill into `~/.claude/skills/altium-boards/`.
6. Puts the `altium-helper` command on your PATH (`uv tool install`).
7. Turns on this repository's git hooks.

**What `login` does** depends on which access route works (see [the routes](#getting-into-altium-365-without-manual-downloads)):

- **A:** opens the browser once to sign in to Altium.
- **B:** asks for your Altium email and password once, and stores them in Git's credential manager.
- **C:** asks for the admin's token.
- **D:** asks for your Nexar app's client ID and secret, then opens the browser.

Restart VS Code afterwards so Claude loads the tools and the skill.

## Use

**Ask Claude.** Open Claude anywhere, for example in the firmware repo, and ask normally. Claude fetches whatever it
needs first.

- "Did my team change the flipped pins on the DAQ board yesterday?"
- "What changed on Ludwig since last Friday? Anything that affects firmware?"
- "On Ludwig, which CM4 GPIOs go to the CAN controllers' SDI, SDO, SCK, CS and INT pins? Compare with
  `embedded/boards/ludwig/configs/firmware/config.txt`."
- "Check `BMSPins.hpp` against the BMS board. List any pin whose net or far-end pin doesn't fit its name."
- "Are there any Altium comments on the PDU about the fuse footprint?"

**The command line** is optional; Claude does all of this itself.

```bash
altium-helper status         # boards you have locally, and when each was last synced
altium-helper sync --all     # fetch every board now, e.g. before going offline
altium-helper login          # sign in again if a token was revoked
```

Board data lives in `designs/`, which git ignores:

```
designs/daq/
  repo/                        git copy of the Altium 365 project, every revision
  revisions/<commit>.netlist.json   netlist for each past revision that has been looked at (cache)
  board.json                   project id, name, git URL, last sync
```

**Updating the tool:**

```bash
cd ~/altium-helper && git pull && altium-helper setup
```

## Git plan and conventions

### This repository

- **Hosting.** A private GitHub repository under your account for now. If the team adopts the tool, transfer it to
  the Penn-Electric-Racing organization; a transfer keeps the history and redirects the old URL.
- **First commit.** The existing research (`CLAUDE.md`, `.gitignore`, `docs/`, `research/`) plus this README, on
  `main`. It only happens after you approve this plan.
- **Branches and pull requests.** `main` always works. Every change gets:
  - a short-lived branch (`feat/altium-login`, `fix/diff-renamed-nets`, `docs/readme`);
  - a pull request that you review;
  - a squash merge, as in the firmware repo.

  Nothing merges without your review.
- **Commit messages.** The subject is imperative and capitalized, at most 72 characters, with no final period
  (`Add revision diff`). Add a body explaining why when that isn't obvious. Commits written with Claude carry a
  `Co-Authored-By: Claude` trailer.
- **Version pins live in git.**
  - Python dependencies: `uv.lock`.
  - universal-netlist: `tools/universal-netlist/package.json` and `package-lock.json`.

  Updating universal-netlist is its own pull request, and it re-runs the accuracy check on the public test boards.
- **Never committed:**
  - board files and anything made from them: `designs/`, `test-designs/`, `*.netlist.json`, `*.PrjPcb`,
    `*.SchDoc`, `*.PcbDoc`, `*.zip`;
  - secrets: `.env`, `*.token`;
  - installed dependencies: `node_modules/`, `.venv/`.

  `.gitignore` covers all of these. A `pre-commit` hook also blocks them, even if someone forces `git add`.
- **Line endings.** `.gitattributes` forces LF, as in the firmware repo, so scripts and hooks run on both Windows and
  Linux.
- **Python style.** black and isort (profile `black`), matching the firmware repo's pre-commit.
- **Versions.** Tag `v0.x.y` on `main` whenever the tool's behavior changes.

### Board repositories in Altium 365

- **One repository per project.** Each Altium 365 project is its own git repository, and the API supplies its URL
  (`repositoryUrl`).
- **Cloned automatically.** A board is cloned into `designs/<board>/repo/` the first time Claude asks about it. Every
  later request runs `git fetch` first, which only downloads new revisions.
- **Which branch.** Only `master`, because Altium 365 tracks `master`. Every Altium 365 save is a git commit, so
  history, authors and messages come straight from git.
- **Past revisions** are read without touching your copy's working files. The tool checks the revision out into a
  temporary folder (`git worktree`), exports its netlist to `designs/<board>/revisions/`, then deletes the folder.
- **Read-only.** Each clone gets two guards: an invalid push URL, and a `pre-push` hook that always fails. The tool
  only runs `clone`, `fetch`, `worktree` and read commands, and never commits.
- **Credentials.**
  - Route A or C tokens are handed to git for each command, never written into the URL or git's config.
  - A route B password lives in Git's credential manager. It comes with Git for Windows; on WSL, setup points git at
    the Windows one.

## Read-only, secrets and privacy

- **Nothing writes to Altium 365.** The API client only sends GraphQL queries, and the code refuses any request that
  contains a mutation. Tokens from Nexar and Altium 365 can write (comments, library parts, users), so this guard
  matters. For git, see the guards above; Altium's git server does accept pushes, so they stay on.
- **universal-netlist** only reads local files. It keeps a usage log of tool calls and file paths that never leaves
  your machine.
- **Secrets.** Tokens, refresh tokens and the Nexar client secret are stored outside the repo: in
  `~/.config/altium-helper/` on Linux and `%APPDATA%\altium-helper\` on Windows, readable only by you.
- **What Claude reads goes to Anthropic.** Some parts may be under sponsor NDA. Every board your account can see is
  reachable, so list any board that mustn't go to an AI provider under `exclude` in the config file, and the tool
  refuses it.

## Next steps

Access comes first, since everything depends on it.

| # | Step | Who | Why |
|---|---|---|---|
| 1 | Sign in at [portal.nexar.com](https://portal.nexar.com) with your Altium account, and paste one query (I'll give it to you) into its web editor | You | Shows whether Nexar works for you (it needs an Altium Designer license, and Altium Designer isn't installed on your PC) and lists every board with its git URL |
| 2 | In a terminal, run `git ls-remote <DAQ git URL>` and enter your Altium email and password when asked | You | Tests route B. Skip it if your Altium account has no password |
| 3 | Email Altium asking them to register altium-helper as a desktop app (I'll draft the email) | You or the team lead | Route A: the supported route, and the slowest step, so start now |
| 4 | Ask the workspace admin whether the workspace has Admin → Developer | You | Route C, only if step 2 fails and step 3 is slow |
| 5 | Once a route works: clone DAQ, Ludwig and BMS, and check universal-netlist against their PCBs | Me | Shows how far the answers can be trusted |
| 6 | Replay real history: find the commit that fixed the MOSI/MISO swap and confirm `diff` shows it. Also check the known mismatches: BMS `canRx`/`canTx` and Ludwig `encA`/`encB`. **Which board and revision had the MOSI/MISO swap?** | Me (you: which board) | Proves the "what changed" answers |
| 7 | On Windows, confirm both MCP servers start from the VS Code extension and the skill loads | Me (I can run Windows programs from WSL) | Windows support |

Build order after approval. Each step is one pull request for you to review.

1. **Repository basics:** the first commit, `.gitattributes`, the hooks, `pyproject.toml` and `uv.lock`, and the
   pinned universal-netlist.
2. **Access:** `login` for whichever route works, the board list, and read-only git sync.
3. **History:** netlists per revision, plus `history` and `diff`.
4. **Claude integration:** the MCP server, the skill and the registration, tested on Windows and WSL.
5. **Checks and comments:** the schematic-vs-PCB check (it reuses the prototype's PCB reader, fixed for
   multi-channel boards) and Altium 365 comments.
6. **Docs:** bring `docs/research.md` and `CLAUDE.md` up to date with the verification results and this plan.

## Known limits and evidence

Known limits:

- **Access is still unproven.** None of routes A–C has been tried with our workspace yet (see Next steps).
- **History only covers what has been saved to Altium 365.** Unsaved work in someone's Altium Designer is invisible.
- **universal-netlist's trace tool** has these quirks. The skill warns Claude about each one, and the PCB check
  catches missed connections.
  - It takes pin *numbers* (`U14.35`), not pin names (`U14.PA1`).
  - It only passes through resistors, capacitors, inductors and ferrites, so it stops at solder bridges and jumpers.
  - It ignores junction dots, so a four-way wire crossing that is joined only by a dot is missed.
  - It treats off-sheet connectors like global power ports.
  - Boards with design variants need a variant name on every call.
- **Nexar** has no past revisions and no schematic wiring. Its PCB data is only as current as the last
  schematic-to-PCB update, and it reportedly has pin names only for parts from the workspace library.

Verified on 2026-09-30:

- **universal-netlist 1.12.0 against each board's PCB pad data.** Net names were identical on every board.

  | Board | Nets matched | Parts matched |
  |---|---|---|
  | ST NUCLEO-144 | 287/287 | — |
  | UBC mainboard | 227/227 | — |
  | UBC breakout (multi-channel sheets and harnesses) | 79/79 | 95/95 |

  `docs/research.md` wrongly says the breakout's PCB is stale. In fact the prototype's PCB reader merged the channel
  copies.
- **Nexar's live schema** has the latest design (PCB pads and nets, pin names on library symbols), releases, a commit
  list (id, author, time, message, changed files), comments and `repositoryUrl`. It has nothing for past revisions
  and no schematic nets.
- **Altium's docs:**
  - git supports clone, pull and push, using HTTP Basic auth with an Altium 365 token issued for the workspace;
  - the Altium 365 API is in closed beta, and only admins create its tokens;
  - whether a token can be read-only is unclear, because two doc pages disagree;
  - registering an app for the desktop sign-in is done together with Altium, with no self-service.
- **universal-netlist's standalone binary** checks for a newer release every time it starts and installs it. There's
  no way to turn that off, and the download isn't checksum-verified. npm installs never update themselves.

## Changes from the earlier plan

The earlier plan is the "Planned architecture" section of [CLAUDE.md](CLAUDE.md).

| Earlier plan | Now | Why |
|---|---|---|
| A team tool: teammates clone a repo that registers the MCP server | A personal tool first, on Windows and Linux; the team later | Your decision (2026-09-30) |
| Fetch with git using an admin token; manual "Download Sources" zip as the fallback | Fully automatic fetch with git, through whichever sign-in route works (A, B or C). Manual zips are dropped | You want no manual downloads |
| — | New: history and diffs ("what changed yesterday?") | Your DAQ example |
| — | New: altium-helper is an MCP server that Claude calls, not only a command line | So Claude can fetch and compare on its own |
| Nexar as optional PCB extras | Nexar as the API for the board list, git URLs, comments and live PCB (unless route A or C gives Altium 365's own API), and as fallback route D | It works with a member's own login |
| Read with universal-netlist, pinned | Same, pinned through npm | The standalone binary can't be pinned |
| A firmware pin-checker program | Ask Claude, guided by the skill; write a program later if needed | Keeps the first tool small |
| Optional sheet images | Not included yet | Not needed to read connectivity or changes |
| — | New: a schematic-vs-PCB check at any revision | Catches reader misses and shows whether the layout followed the schematic |

**Team use later.** The same repo moves to the team organization, and each teammate runs `setup` and `login`. With
route A everyone signs in as themselves. The firmware repo gets a short AGENTS.md or CLAUDE.md note pointing here.

## Repository layout (planned)

```
altium-helper/
  README.md, CLAUDE.md
  pyproject.toml, uv.lock          altium-helper's Python package and pinned dependencies
  src/altium_helper/               mcp_server, auth, api, sync, history, diff, checks, setup, cli
  tools/universal-netlist/         package.json + package-lock.json (pinned); node_modules/ is git-ignored
  skill/SKILL.md                   template for ~/.claude/skills/altium-boards/
  hooks/                           pre-commit: blocks board files and secrets
  docs/research.md                 research and evidence
  research/prototype/              throwaway feasibility scripts, kept as a cross-check
  designs/                         your boards: git copies and netlist cache (git-ignored)
  test-designs/                    public test boards, downloaded by a script (git-ignored)
```
