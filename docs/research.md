# Research notes

Facts the design relies on. Everything here was checked directly on 2026-09-30, by reading the documentation page,
introspecting the live API schema, reading the source code, or running the tool. Anything that wasn't is marked
**unverified**.

## 1. Getting design data out of Altium 365

### Git (the only automatic source of files and history)
- Altium 365 hosts each project's source files in a git repository. You can "clone, pull, push, etc. with the
  standard Git client". So **push works too**, and read-only has to be enforced by us.
  ([Project Git Access](https://www.altium.com/documentation/altium-developer-center/altium-365/key-concepts/project-git-access), updated 2026-08-28.)
- Get the repository URL from the API's `desProjects { repositoryUrl }`, and treat it as an opaque value.
- Authentication is HTTP Basic. The username is ignored but must be non-empty. The password is "your Altium 365 API
  access token", and "the token must be issued for the Workspace that owns the project".
- The platform tracks `master`. The git server rejects `.zip`/`.7z`/`.rar` files larger than 256 MB.
- A public 2023 mirror job cloned `https://<email>:<password>@afs-vcs-eu1.365.altium.com/git/<GUID>.git`, i.e. with
  an account email and password
  ([mirror.yml](https://github.com/atochukwu0/SISPS-PV-PCB/blob/master/.github/workflows/mirror.yml)). It still
  works: our workspace's git accepted an account email and password on 2026-10-03.

### Altium 365 API tokens
- The API is in closed beta: "currently in Closed Beta, and available only to a few selected early-access customers"
  ([quick start](https://www.altium.com/documentation/altium-developer-center/quick-starts/365-api)).
- "Only Workspace administrators can create tokens", under Admin → Developer. Tokens last up to 1 year. Refresh-token
  clients produce 1-hour access tokens
  ([tokens](https://www.altium.com/documentation/altium-developer-center/altium-365/key-concepts/tokens)).
- Whether a token can be limited to reading is unclear, because two pages disagree. The Tokens page says "Scopes are
  configured when the token is created". The
  [OAuth Scopes](https://www.altium.com/documentation/altium-developer-center/altium-365/key-concepts/oauth-scopes)
  page says "You don't select scopes manually".

### Altium sign-in for desktop apps (access route A)
- [altium-auth](https://github.com/AltiumDeveloper/altium-auth) is Altium's official auth library, MIT-licensed,
  0.2.0 preview. It documents:
  - OAuth2/OIDC with PKCE against `auth.altium.com`;
  - the "ActionWait" flow for desktop apps: the redirect goes to an Altium-hosted page and the app long-polls
    `actionwait.altium.com`, so there's no local web server and no secret;
  - an RFC 8693 token exchange for a workspace token (`a365:workspace:{id}`).
- The catch: "Application registration is currently a guided process handled together with Altium — there is no
  self-service registration portal."

### Nexar
- Endpoint: `https://api.nexar.com/graphql`. The schema can be introspected anonymously.
- **Design data:** available for the latest design only, plus releases.
  - `desProjectById → design.variants[].pcb`: `designItems { designator, pads { designator, net { name } } }`, and
    `nets`.
  - Pin names come from `component.details.symbols.pins` and are **reportedly** present only for workspace-library
    parts.
- **History and comments:**
  - `DesProject.revisions` / `latestRevision`: `revisionId`, `message`, `author`, `createdAt`, and `files { path,
    kind }`.
  - `desCommentThreads(projectId)`.
- **Missing:** schematic nets (`DesSchematic` only has `designItems`) and any design data for a past revision. No
  field takes a revision argument.
- **Writes are possible:** there are 136 mutations (comments, tasks, library parts, users, permissions, project
  upload). `desCreateProjectExportJob` runs an OutJob in Altium's cloud; it's a mutation, so it's not used.
- **Auth:** OAuth2 authorization code with PKCE. The official Python example needs both a client ID and a client
  secret, redirects to `http://localhost:3000/login`, and supports refresh tokens.
  - Scopes: `openid profile email design.domain user.access`.
  - Access tokens last 24 hours.
  - New accounts get an "Evaluation App" with the Design scope.
- **Licensing:** "Design Queries requires you to have an Altium Designer license and to be a member of a workspace"
  ([FAQ](https://support.nexar.com/support/solutions/articles/101000497890-frequently-asked-questions)). With both,
  design queries are free and unlimited. The Evaluation App's lifetime limit of 1000 parts counts Supply queries
  only (FAQ, read 2026-10-03).

### Manual download (not used: the tool must not need manual steps)
- In Altium 365's History view, commit and release entries have **Download Sources**, which gives a zip of that
  revision ([project history](https://www.altium.com/documentation/altium-365/project-history)).

## 2. Reading Altium files without Altium

### universal-netlist
- Repository: [universal-netlist](https://github.com/IntelligentElectron/universal-netlist). Apache-2.0, maintained
  mostly by one person, with 15 releases between 2026-08-19 and 2026-09-26. We pin 1.12.0.
- It is an MCP server and a CLI (`export-json <design> out.netlist.json`). It reads `.PrjPcb`/`.SchDoc` and its own
  versioned `.netlist.json` format.
  - The format hashes `nets` and `components` with SHA-256, over canonical JSON with sorted keys.
  - Pins are stored as `{"name", "net"}` objects when the pin name differs from the pin number.
- **Updates:**
  - The standalone GitHub binary self-updates on every server start. There is no switch to turn that off, and the
    download isn't checksum-checked.
  - npm installs never self-update. It needs Node 20 or newer.
- **Quirks** (confirmed in the source or by running it):
  - `query_xnet_by_pin_name` accepts `REFDES.PINNUMBER` only.
  - Tracing passes through R, L, C and FB parts only, so it stops at SB and JP.
  - Junction records (type 29) are not parsed.
  - Off-sheet connectors are treated like power ports.
  - Designs with variants require `design_variant` on every call.
  - It keeps a local usage log (`telemetry.jsonl`); `UNIVERSAL_NETLIST_TELEMETRY_PATH` moves it.

### Accuracy tests
Method: export the schematic netlist, then compare every net that has 2 or more pads with the pad-to-net data inside
the same project's `.PcbDoc`. The test boards are public, from
[UBC-Thunderbots/PCB_MainBoard](https://github.com/UBC-Thunderbots/PCB_MainBoard) (no license; download and use
locally only).

| Board | Sheets | Nets matched | Parts matched | Names identical |
|---|---|---|---|---|
| ST NUCLEO-144 (mb1364) | 8 | 287/287 | — | yes |
| UBC mainboard v2 | 14 | 227/227 | — | yes |
| UBC breakout (multi-channel sheets and harness-typed ports) | 8 | 79/79 | 95/95 | yes |

An earlier note called the breakout's PCB "stale". That was wrong. The PCB reader keyed parts by
`SOURCEDESIGNATOR`, which is the logical designator, so the channel copies were merged together.
- Physical designator = `SOURCEDESIGNATOR + "_" + last element of SOURCEHIERARCHICALPATH`.
- That matches the project's `ChannelDesignatorFormatString=$Component_$RoomName`.

What's untested: `Repeat()` channels, harness connectors drawn on a sheet, off-sheet connectors, and the Flat/Global
scope modes.

### Not used
- **MCP servers built on Altium Designer** (coffeenmusic/altium-mcp, salitronic/eda-agent, ee-in-a-box/altium-copilot):
  they need Altium Designer running on Windows.
- **KiCad** (**unverified**, from earlier research): released `kicad-cli` can't import `.SchDoc`. KiCad 10 imports
  `.PcbDoc`.
- **Rendering sheets:** python-altium can render SVG (it needs a 2-line patch on Python 3.10+). Not needed yet.

## 3. Found while building (2026-09-30)

- **The Universal Netlist hash** is `SHA-256(JSON.stringify(canonical {nets, components}))`, computed in
  JavaScript.
  - JavaScript objects list integer-like keys (pin numbers) first and in numeric order, so a plain sorted-keys
    JSON dump gives the wrong hash.
  - Schema version 2 allows only these component fields: `mpn`, `internal_pn`, `manufacturer`, `description`,
    `comment`, `value`, `dns` (true only) and `pins`. The reader drops any other field before checking the hash.
  - `unformat.py` reproduces the hash exactly on real exports.
- **Nexar details** (live schema):
  - `desWorkspaces` is deprecated; use `desWorkspaceInfos { name url isDefault }`.
  - `desProjects(workspaceUrl, first, after)` is paginated and carries `repositoryUrl`.
  - `desComponentsByIds` returns a `DesComponent | DesErrorPayload` union.
  - `desCommentThreads(projectId)` returns the threads with their comments.
  - The identity server (`identity.nexar.com`) supports the `offline_access` scope and the refresh-token and
    device-code grants.
- **Git performance on the public UBC repo** (167 MB, many boards; GitHub, which has partial clone):
  - A blobless clone takes 0.5 s and 300 KB. Altium 365's git server can't do this; see section 4.
  - Checking out one board folder at an old revision into a sparse worktree takes 0.75 s and about 5 MB.
  - Git 2.36 or newer is needed for `sparse-checkout --no-cone`.
- **Real history.** The diff engine read UBC's "Reconfigured MCU pinout" commit as one pin swap, one 3-pin rotation
  and six moves. It read "Replaced 3V3 power select diodes with jumpers" as D2 removed and 0 Ω R104/R105 added.
- **A real Claude session** (headless `claude -p` with only these tools) got both kinds of question right:
  - It explained that commit, and noticed the PCB wasn't updated in it.
  - It traced SPI1 on the mainboard through series resistors to its far end.
  - It loaded the `altium-boards` skill by itself.
- **MCP Python SDK 2.x** renamed `FastMCP` to `MCPServer` (`mcp.server.mcpserver`). `ToolError` lives in
  `mcp.server.mcpserver.exceptions`.
- **Node without installing Node.** `nodejs-wheel-binaries` (MIT) ships Node 24 as a Python wheel for Linux (glibc
  2.28+ and musl), macOS and Windows. That makes Git and uv the only prerequisites.

## 4. Our workspace (2026-10-03)
- **Access.** `altium-helper login` worked with a newly created Nexar app (Design scope): the localhost redirect
  and `offline_access` were accepted, and Nexar listed every project with a `repositoryUrl`. Git then accepted the
  account's email and password (route B). Nexar answering design queries implies the account has an Altium
  Designer license.
- **Repositories** are on `https://afs-vcs-ue1.365.altium.com/git/<id>.git`. The first board read had its
  `.PrjPcb` at the repository root, `master` as its branch, and no Git LFS.
- **Commit messages are often empty**: Altium doesn't ask for one when a project is saved to the server, and none
  of that board's commits had one. `str.strip()` treats git's `\x1f` separator as whitespace, which broke parsing.
- **Comment threads:** `DesCommentThread.status` is an `Int`; the schema says "0 = Resolved, 1 = Active".
- **Parallel tool calls run at once.** MCP SDK 2.x handles requests concurrently and runs plain tools on worker
  threads. Two calls on a board nobody had fetched both cloned it, and one failed.
- **The workspace has 706 projects**, all with a git URL, so fetching every board up front isn't practical.

### Altium 365's git server
Measured on REV12 PDU 2 (68 commits), BMS Master (269) and PCM (998), and a FlashCat adapter (6).
- **What it is.** Git for Windows 2.45.1, behind an AWS load balancer, Envoy and ASP.NET MVC on IIS. It speaks
  protocol v0 only, and advertises `shallow deepen-since deepen-not deepen-relative` but **no `filter`** and no
  `allow-tip-sha1-in-want` or `allow-reachable-sha1-in-want`.
- **No partial clone.** `clone --filter=blob:none` silently downloads every version of every file (`--quiet` hides
  git's "filtering not recognized by server" warning): 46 / 103 / 131 MB for PDU / BMS / PCM. Old versions are
  67-93% of that, mostly old `.PcbDoc` files.
- **No fetch by commit id:** "Server does not allow request for unadvertised object". Older history comes by count
  (`--deepen`) or date (`--shallow-since`).
- **It builds each download from scratch.** Recent commits are stored unpacked (PDU had never been repacked), so
  every request compresses them again, 3-13 times slower than a laptop does. Nothing is sent until the download is
  ready, then it arrives at 31-42 MB/s: a full clone was 40-56 s of silence and 1-4 s of transfer. Our own
  bandwidth (34 MB/s) isn't the limit.
- **Timings** (one at a time; the first runs, in the morning, were 2-5 times slower):

  | | PDU 2 | BMS Master | PCM |
  |---|---|---|---|
  | Full clone | 43-54 s, 46 MB | 48-55 s, 103 MB | 57-62 s, 131 MB |
  | Latest revision only (`--depth=1`) | 9.5-13 s, 15 MB | 9-20 s, 17 MB | 7-13 s, 9 MB |
  | `--deepen=1` | 5.2 s | 3.3 s | 3.9 s |
  | A busy day (`--deepen` by 12 to 17 commits) | 22.0 s | 18.8 s | 13.6 s |
  | The rest of the history (`--unshallow`) | 39-41 s | 47-50 s | 52 s |

- **Later fetches send only what's new**, as deltas against what the copy holds. A `.PcbDoc` change arrives as
  130-225 KB instead of a 4-5 MB copy. A fetch with nothing new takes 0.42 s; one new commit 2.7-5.7 s; 20 new
  commits 22 s. Each request costs about 2-4 s plus 0.15-0.25 s per object the server compresses.
- **Nexar's revision list is git's history:** the same ids, order, authors, dates, messages and changed files on
  all 1,341 commits of these four boards (a rename shows as a change to the new path, or as a delete plus an
  add), in 1-3 s per board. Paths start with the project's GUID (`\<GUID>\<path>`), and dates are in UTC.

## 5. Open questions
1. Will Altium register altium-helper as a public desktop client (route A)?
2. Does our workspace have Admin → Developer (route C)? Members without an Altium password would need it.
3. Does any of our repositories use Git LFS? If so, clones need `git-lfs`. The first board read didn't.
4. Which board and revision had the CAN-SPI MOSI/MISO swap?
5. Would Altium repack our repositories, or enable `uploadpack.allowFilter`? Either would make first fetches much
   faster.
