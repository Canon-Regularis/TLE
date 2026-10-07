# TLE Architecture Document

## Overview

TLE (Time Limit Exceeded) is a Discord bot for competitive programming communities, built around the Codeforces platform. It provides problem recommendations, contest tracking, dueling, performance visualization, and community management features.

**Tech Stack:** Python 3.10+, discord.py 2.x, aiosqlite, aiohttp, matplotlib/seaborn, numpy, Pillow, PyCairo/PyGObject, PyJWT

---

## High-Level Architecture

```
Discord Gateway
       |
       v
+------------------+
|   Bot Runtime    |  tle/__main__.py
|  (commands.Bot)  |  - Entry point, arg parsing, cog loading
+------------------+
       |
       +--- bot.user_db          (UserDbConn)
       +--- bot.cf_cache         (CacheSystem)
       +--- bot.event_sys        (EventSystem)
       +--- bot.oauth_server     (OAuthServer, optional)
       +--- bot.oauth_state_store (OAuthStateStore, optional)
       +--- bot.kcpc             (KcpcServices, see §10)
       +--- bot.access           (AccessService, see §11)
       |
       v
+------------------+     +-------------------+
|     Cogs (9)     |---->|   Utility Layer   |
| (Command Groups) |     | codeforces_common |
+------------------+     | discord_common    |
       |                  +-------------------+
       |                         |
       v                         v
+------------------+     +-------------------+
|  Cache System    |     |   Database Layer  |
| util/cache/      |     | (aiosqlite)       |
| (5 sub-caches)   |     | user_db_conn.py   |
+------------------+     | cache_db_conn.py  |
       |                  +-------------------+
       v                         |
+------------------+             v
| Codeforces API   |     +-------------------+
| codeforces_api.py|     |     SQLite3       |
+------------------+     | data/db/user.db   |
                         | data/db/cache.db  |
                         +-------------------+
```

---

## Directory Structure

```
TLE/
├── tle/
│   ├── __init__.py
│   ├── __main__.py              # Entry point: bot setup, cog loading, initialization
│   ├── config.py                # Settings read from the environment (KCPC's, ALLOWED_GUILD_IDS)
│   ├── constants.py             # Paths, role names, env config, feature flags, OAuth config
│   ├── extensions.py            # Which extensions load (DISABLED_EXTENSIONS)
│   ├── access/                  # Who may use each command, and where (see §11)
│   │   ├── rules.py             # The model and decide(): no Discord imports
│   │   ├── table.py             # Every command's default rule, twins, /help categories
│   │   ├── settings.py          # A server's channels and limits, and their JSON
│   │   ├── policy.py            # A command's rule in one server; rules in plain English
│   │   ├── service.py           # AccessService (the check), AccessTree
│   │   ├── context.py           # TLEContext: keeps private answers private
│   │   ├── slash.py             # Hides staff commands from members' slash lists
│   │   ├── help.py              # /help
│   │   └── cog.py               # /access
│   ├── kcpc/                    # KCPC club features (see §10)
│   ├── cogs/                    # Discord command modules (Cog pattern)
│   │   ├── cache_control.py     # The bot owner's commands that reload the Codeforces caches
│   │   ├── codeforces.py        # Problem recommendations, gitgud, upsolve, mashup
│   │   ├── contests.py          # Contest listing, reminders, rated virtual contests
│   │   ├── duel.py              # 1v1 dueling system with ELO ratings
│   │   ├── graphs.py            # matplotlib/seaborn visualizations
│   │   ├── handles.py           # Handle registration, role management, rank updates
│   │   ├── logging.py           # Discord channel logging handler
│   │   ├── meta.py              # Ping and uptime; git history; the owner's kill and server list
│   │   └── starboard.py         # Reaction-based message archival
│   └── util/
│       ├── __init__.py
│       ├── codeforces_api.py    # CF API wrapper with rate limiting and data models
│       ├── codeforces_common.py # Shared logic: handle resolution, filtering, globals
│       ├── discord_common.py    # Embed helpers, error handler, presence system
│       ├── events.py            # Pub/sub event system for inter-component communication
│       ├── graph_common.py      # matplotlib setup, BytesIO plotting, rating backgrounds
│       ├── handle_linking.py    # Links handles: TLE's table, rank roles, Purgatory/Trusted (;handle set, OAuth, /link)
│       ├── handledict.py        # Case-insensitive handle dictionary
│       ├── oauth.py             # Codeforces OAuth (OIDC) state store, token handling, callback server
│       ├── paginator.py         # Paged replies with buttons, which only the member who asked can turn
│       ├── table.py             # ASCII table formatter
│       ├── tasks.py             # Custom async task framework (Task, TaskSpec, Waiter)
│       ├── cache/               # Modular cache system (split from former cache_system2.py)
│       │   ├── __init__.py      # Re-exports CacheSystem and error types
│       │   ├── _common.py       # Shared cache utilities
│       │   ├── cache_system.py  # CacheSystem orchestrator
│       │   ├── contest.py       # ContestCache
│       │   ├── problem.py       # ProblemCache
│       │   ├── problemset.py    # ProblemsetCache
│       │   ├── ranklist.py      # RanklistCache
│       │   └── rating_changes.py # RatingChangesCache
│       ├── db/
│       │   ├── __init__.py      # Re-exports db connections
│       │   ├── cache_db_conn.py # Async cache for CF API data (aiosqlite)
│       │   └── user_db_conn.py  # Async user data: handles, duels, challenges, starboard
│       └── ranklist/
│           ├── __init__.py
│           ├── ranklist.py      # Contest ranklist construction and querying
│           └── rating_calculator.py  # FFT-based CF rating calculator
├── extra/
│   └── scrape_cf_contest_writers.py
├── data/                        # Runtime data (gitignored)
│   ├── db/                      # SQLite databases
│   ├── misc/                    # contest_writers.json
│   └── temp/                    # Temporary plot images
├── logs/                        # Rotating log files (gitignored)
├── .github/workflows/
│   ├── build.yaml               # Docker build CI
│   └── lint.yaml                # Ruff linting CI
├── pyproject.toml               # PEP 517 project config with pinned dependencies
├── ruff.toml                    # Linting configuration
├── Dockerfile                   # Multi-stage Python 3.11-slim container
├── docker-compose.yaml          # Single-service deployment
├── .env                         # Bot token and config (gitignored)
└── .gitignore
```

---

## Component Deep-Dive

### 1. Bot Runtime (`tle/__main__.py`)

The entry point performs:
1. Loads `.env` with `python-dotenv` (when `tle.constants` is imported)
2. Parses the `--nodb` CLI flag and reads the settings (`tle.config.Settings`); a bad setting, such as an `ALLOWED_GUILD_IDS` item that isn't an ID, stops it with an error
3. Creates required data directories
4. Configures logging (console + daily rotating file)
5. Sets up matplotlib/seaborn defaults
6. Creates a `TLEBot(commands.Bot)` with prefix `;` (or mention), member intents, and `message_content` intent. Its constructor sets up the access rules (§11): `help_command=None`, the `AccessTree` command tree, an `AccessService` as `bot.access` added as the bot-wide check, and `bot_error_handler` as the `on_command_error` listener. It also sets `allowed_mentions` (members can be pinged, never @everyone, roles or the author a reply answers, unless a message allows it), and makes slash commands work in servers alone (`allowed_contexts`) and the app installable in servers alone (`allowed_installs`)
7. Runs `setup_hook()` (below) before connecting
8. On the first ready: logs the servers that `ALLOWED_GUILD_IDS` doesn't list (it never leaves them) and starts the presence task, which shows members of allowed servers only. `on_guild_join` leaves a newly joined server that isn't listed
9. Overrides `close()` to shut KCPC down and close the OAuth server and the database connections

**`setup_hook()` runs, in this order:**
1. the logging extension, first, so that problems while starting up reach the log channel; then the warning about an unusable `TLE_DEVELOPER`, now that logging is set up
2. `cf_common.initialize(bot, nodb)`: database connections, cache system and event system, as `bot.user_db`, `bot.cf_cache` and `bot.event_sys`
3. the access settings: `access.use_user_db(None if nodb else bot.user_db)` and `access.load()`
4. the Access and Help cogs: core cogs, not extensions, so `DISABLED_EXTENSIONS` never removes `/access` and `/help`
5. KCPC's services (§10)
6. the extensions, TLE's and KCPC's
7. the OAuth callback server, if configured
8. `access.resolve_owners()`, guarded: the bot's owners, from `application_info()`
9. `access.report_unruled(...)`: a WARNING for each command without a rule
10. `apply_visibility(bot)`: the slash pass (§11)
11. `tree.sync()`

All of this happens before the bot connects to Discord, so every command is checked from the first event on. `tests/kcpc/component/test_boot.py` pins the order, and `tests/kcpc/component/booting.py` boots the real bot for tests, with stand-ins for Discord and the database files.

**Error replies** (`discord_common.bot_error_handler`) are private on slash, and skipped once the interaction has expired. An access refusal (`AccessDenied`) is silent when the member may not use the command at all on prefix; on prefix, refusals are deleted after 20 seconds and throttled to one per member per 30 seconds, and cooldown notes go when the cooldown ends. Any other failed check gets the generic "You can't use this command.", never a role's name or ID, and an unexpected error gets "Something went wrong. The error has been logged."

### 2. Cog Layer (`tle/cogs/`)

Each cog is a `commands.Cog` subclass that groups related commands. Cogs access services via `self.bot.user_db`, `self.bot.cf_cache`, and `self.bot.event_sys`:

```python
class MyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(brief="Show a member's Codeforces rating")
    @app_commands.describe(member='The member whose rating to show; you if left out')
    async def rating(self, ctx, member: discord.Member | None = None):
        """Show a member's Codeforces rating: yours, if you name no one.

        Examples:
            /rating
            /rating member:@alice
            ;rating @alice
        """
        await ctx.send(...)  # never ctx.channel.send: see §11

    @discord_common.send_error_if(MyCogError)
    async def cog_command_error(self, ctx, error):
        pass

async def setup(bot):
    await bot.add_cog(MyCog(bot))
```

A command carries no role check of its own: its rule in `tle/access/table.py` decides who may use it and where (§11), and every new command needs one. Its texts follow the style guide below.

| Cog | Commands | Responsibility |
|-----|----------|---------------|
| **CacheControl** | 5 | The bot owner's commands that reload the Codeforces caches |
| **Codeforces** | 12 | Problem recommendation, gitgud challenges, upsolve, mashup, team rating |
| **Contests** | 17 | Contest listing, reminders, ranklist, rated virtual contests |
| **Dueling** | 18 | 1v1 challenges with ELO rating, draws, history, rankings |
| **Graphs** | 14 | Rating plots, solve history, distributions, country comparisons |
| **Handles** | 20 | Handle linking (via Codeforces OAuth), role management, rank updates, trusted roles |
| **Logging** | 0 | Background log handler sending warnings to a Discord channel, with no pings |
| **Meta** | 6 | Ping and uptime; git history for developers; the owner's kill and server list |
| **Starboard** | 8 | Multi-emoji reaction archival with configurable thresholds, never from the staff channel |

The counts include each group's own command. `/help` and `/access` come from the Help and Access cogs in `tle/access/`, which the bot adds itself (§11).

### 3. Cache System (`tle/util/cache/`)

The cache system is organized as a package with each cache in its own module, coordinated by `CacheSystem` in `cache_system.py`:

```
CacheSystem (cache_system.py)
├── ContestCache      (contest.py)       # All CF contests, refreshes every 30m (5m when active)
├── ProblemCache      (problem.py)       # Problemset with ratings/tags, refreshes every 6h
├── ProblemsetCache   (problemset.py)    # Per-contest problems from standings, monitors 14 days post-finish
├── RatingChangesCache (rating_changes.py) # Rating changes for finished contests, monitors up to 36h
└── RanklistCache     (ranklist.py)      # Standings with predictions for running contests
```

Shared utilities live in `_common.py`. The `__init__.py` re-exports `CacheSystem` and error types for clean imports.

Each cache uses the custom `TaskSpec` framework (not discord.py's `tasks.loop`) for periodic updates with dynamic delays. Caches persist to SQLite (via `CacheDbConn`) and reload from disk on startup for fast restarts.

**Event flow:** When `RatingChangesCache` detects new rating changes, it fires a `RatingChangesUpdate` event via `EventSystem`, which `Handles` cog listens to for automatic rank role updates.

### 4. Database Layer (`tle/util/db/`)

Two SQLite databases accessed asynchronously via `aiosqlite`, with direct parameterized SQL queries (no ORM). Connections use a two-step initialization pattern: `__init__(path)` followed by `async connect()`.

```python
# Initialization in cf_common.initialize()
user_db = db.UserDbConn(constants.USER_DB_FILE_PATH)
await user_db.connect()  # Opens aiosqlite connection and creates tables

cache_db = db.CacheDbConn(constants.CACHE_DB_FILE_PATH)
await cache_db.connect()
```

**`user.db`** (via `UserDbConn`) - 16 tables:
- `user_handle` - Discord-to-CF handle mapping (guild-scoped)
- `cf_user_cache` - Cached CF user profiles
- `duelist`, `duel` - Duel system with ELO ratings
- `challenge`, `user_challenge` - Gitgud challenge tracking
- `reminder` - Contest reminder settings per guild
- `rankup`, `auto_role_update` - Role update configuration
- `rated_vcs`, `rated_vc_users`, `rated_vc_settings` - Virtual contest rating
- `starboard_config_v1`, `starboard_emoji_v1`, `starboard_message_v1` - Starboard
- `access_settings` - Each server's access settings (bot channels, staff channel, limits) as one versioned JSON document (§11)

**`cache.db`** (via `CacheDbConn`) - 4 tables:
- `contest` - Cached contest metadata
- `problem` - Problem metadata with JSON-serialized tags
- `problem2` - Problemset-specific problem data
- `rating_change` - Historical rating changes

All database methods are async and all call sites use `await`.

### 5. Codeforces API Client (`tle/util/codeforces_api.py`)

A full async wrapper around the Codeforces REST API:

- **Data Models:** 10 NamedTuple classes (`User`, `Problem`, `Contest`, `Submission`, `RatingChange`, `Party`, `Member`, `RanklistRow`, `ProblemResult`, `ProblemStatistics`)
- **Rate Limiting:** 1 request/second with 3 retries on `CallLimitExceeded`
- **Session Management:** Global `aiohttp.ClientSession` initialized once
- **Handle Resolution:** Batch redirect detection for renamed accounts
- **Endpoints:** `contest.list`, `contest.ratingChanges`, `contest.standings`, `problemset.problems`, `user.info`, `user.rating`, `user.ratedList`, `user.status`

### 6. Event System (`tle/util/events.py`)

A pub/sub system enabling loose coupling between components:

```python
# Publisher (in cache modules)
cf_common.event_sys.dispatch(events.ContestListRefresh, contests)

# Subscriber (in cogs, via task framework)
@tasks.task_spec(name='...', waiter=tasks.Waiter.for_event(events.ContestListRefresh))
async def _update_task(self, _):
    ...
```

Events: `ContestListRefresh`, `RatingChangesUpdate`

### 7. Custom Task Framework (`tle/util/tasks.py`)

A custom alternative to `discord.ext.tasks` providing:
- **`Task`**: Repeating async task with waiter, exception handler, manual trigger
- **`TaskSpec`**: Descriptor-based task that auto-creates per-instance tasks
- **`Waiter`**: Pluggable wait strategies (fixed delay, event-based, custom)

This framework is used throughout the cache system and by background maintenance tasks.

### 8. OAuth / Codeforces OpenID Connect (`tle/util/oauth.py`)

The `identify` command uses Codeforces's OpenID Connect (OAuth 2.0) flow to verify handle ownership. This replaces the older compile-error verification method with a one-click authorization link.

**Components:**
- **`OAuthStateStore`** — In-memory `dict[str, OAuthPending]` mapping state tokens to `(user_id, guild_id, interaction)` with 5-minute TTL and single-use consumption. `interaction` is the slash command's, or None for `;handle identify`
- **`OAuthServer`** — aiohttp web server (default port 8080) with a `/callback` route that handles the authorization code exchange
- **Helper functions:** `build_auth_url()`, `exchange_code()`, `decode_id_token()` (HS256 via PyJWT with issuer/audience/expiry validation)

**Flow:**
```
User runs /handle identify (or ;handle identify)
  -> Bot generates state token, stores mapping with the slash command's interaction
  -> Bot answers privately with a "Sign in to Codeforces" link button
     (;handle identify sends it by direct message)
  -> User clicks, logs in on CF, authorizes
  -> CF redirects to /callback?code=...&state=...
  -> OAuthServer exchanges code for ID token at CF token endpoint
  -> Decodes ID token (HS256) -> extracts handle
  -> Fetches full CF user info via cf.user.info()
  -> Calls handle_linking.link_handle(bot.user_db, guild, member, user), which finds the rank role before writing anything
  -> Tells the member how it went, privately: an ephemeral followup through the stored interaction,
     or a direct message after ;handle identify; never a post in a channel
  -> Returns success HTML to browser
```

If the member can't be told (direct messages closed, or the interaction expired), that is logged at INFO and nothing else is sent; the account stays linked.

A link that `link_handle` refuses (`HandleLinkError`: another member has the handle, or the server has no role for its rank) fails the same way each time, so the member, and the page, are told its reason and to ask a moderator; it is logged at INFO. Any other failure is logged as an error, and the member is told to try again.

**Configuration:** Requires `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_REDIRECT_URI` environment variables. When not set, `constants.OAUTH_CONFIGURED` is `False` and the `identify` command shows a configuration error. The server is only started when OAuth is configured.

### 9. Visualization (`tle/util/graph_common.py` + `tle/cogs/graphs.py`)

Generates matplotlib/seaborn plots as Discord file attachments:
- Rating history over time (by contest or date)
- Solve statistics and histograms
- Performance scatter plots
- Rating distributions (server-wide and global CF)
- Country comparisons
- Speed analysis

Plots are rendered to in-memory `BytesIO` buffers (not temp files on disk) and sent as Discord `File` attachments. Cairo/Pango is used for advanced text rendering (handle lists with rating colors). CJK fonts are installed as system packages in the Docker image (`fonts-noto-cjk`).

### 10. KCPC Club Features (`tle/kcpc/`)

The KCPC club's own features live in `tle/kcpc/`, apart from TLE's code. They load as extensions named `kcpc.<feature>` (`KCPC_EXTENSIONS` in `tle/extensions.py`), which `DISABLED_EXTENSIONS` switches off one by one, or all at once with `kcpc`. In `setup_hook`, after `cf_common.initialize()`, the bot builds KCPC's services with `tle.kcpc.bootstrap.build_services()`, attaches them as `bot.kcpc`, and then loads the extensions. A KCPC extension that fails to load is logged and left out, and the bot carries on without it; with `--nodb`, or if the services can't start, none loads.

**Layers.** The package is layered, lowest first (`tle/kcpc/__init__.py` has the overview), and each layer imports only from the layers below it:
- `core/`: infrastructure without Discord: an injectable clock (tests drive a `FakeClock`), schedules and the job scheduler, the database and its migrations, the delivery ledger, the reminder engine, per-server settings, the paced HTTP client, and the registry through which features read members' linked handles
- `bot/`: the Discord toolkit that KCPC cogs share: the base cog `KcpcCog` (error replies, `self.services`), checks, embeds, views, pages, the plumbing that attaches each feature's admin commands under `/kcpc`, and `DiscordPublisher`. `bot/codeforces_links.py` is KCPC's only way to TLE's user database and handle linking, for members' Codeforces handles
- `platforms/`: adapters for external sites: Luma, AtCoder (contests, profiles, editorials, AtCoder Problems), icpc.global, clist.by, and Codeforces through TLE's own client and caches
- `features/`: one package per feature, a thin `cog.py` (and `views.py`) over services and repositories without Discord. Features never import each other

`tests/kcpc/unit/test_architecture.py` enforces these rules by parsing every import in `tle/kcpc`, deferred ones included. It also checks that only `bot/codeforces_links.py` reaches TLE's user database, and only the contests feature TLE's event system.

**KCPC and the access rules.** No module under `tle/kcpc` imports `tle.access`, not even under `TYPE_CHECKING`; `test_architecture.py` enforces that too. KCPC reaches the access service only as `getattr(bot, 'access', None)`, typed locally (the Verify button uses a small `Protocol`), and works without it. The access rules decide first, for KCPC's commands as for TLE's (§11); KCPC keeps its own checks on its admin commands, `kcpc_admin_only()` and `kcpc_status_only()` (admins, or the `TLE_DEVELOPER` role), as a second line of defence. They are on each command, since discord.py doesn't run a hybrid group's checks for its subcommands. The club contest commands are the bot owner's, and keep `kcpc_admin_only()`, so their rule asks for an owner who is also an admin (`table.OWNER_AND_ADMIN`), and /help says so.

**Services.** `KcpcServices` (`services.py`) is one typed container: the settings, clock, database, HTTP client, feature registry and per-server settings, delivery ledger, publisher, reminder engine, scheduler and handle registry. On shutdown it stops the scheduler, then closes the HTTP client, then the database.

**kcpc.db.** KCPC keeps its data in a database of its own, `data/db/kcpc.db` (`KCPC_DB_PATH`). `core/db.py` wraps one aiosqlite connection: every statement takes one lock, a transaction belongs to the task that opened it, and the database runs in WAL mode with `synchronous = FULL` and foreign keys on. Numbered migrations build its schema (`core/migrations/m0001_core.py` to `m0007_contest_results.py`), each in one transaction recorded in `schema_version`. The file is backed up as `kcpc.db.v<N>.bak` before an upgrade, and a database newer than the code is refused.

**Scheduler.** `core/scheduler.py` runs each `ScheduledJob` at the slots of its schedule (`Every`, `Weekly`, `Monthly`), in a task of its own, once the bot is ready. A persistent job records its last slot in `job_state`: a fresh install skips its first slot, a restart catches up the latest missed slot within the job's grace, and a failed slot is retried. A non-persistent job, such as a sync, keeps its state in memory. An unexpected error is logged, and the job carries on. `/kcpc status` lists the jobs and their last errors to the bot owner alone, on slash, where the answer is private; admins and developers see the server's post counts and skipped posts.

**At-most-once posts.** Every automatic post goes through `services.publisher`, outside any database transaction, as an `OutgoingMessage` with one or more `Delivery` keys (one reminder in one server, say). The publisher checks that the feature is on and has a channel it can post in, claims the keys in `delivery_log` (`core/ledger.py`) in a transaction committed before it sends, then sends the post and confirms the claim. A key already in the ledger is never posted again. The `kcpc.reconcile` job settles every 2 minutes the claims whose send timed out or was cut short: it looks for the post in its channel, by the ref marker in its footer, and confirms it, sends it again or gives it up. A channel that is missing or lacks permissions is reported to the log channel, at most once a day.

**Reminder engine.** `core/reminders.py` reminds members of upcoming occurrences for every feature that registers a `ReminderSource`: its occurrences in a time window, its policy from the server's settings (reminder offsets, a post at the start) and the look of each post. The `kcpc.reminders` job plans every server's posts each minute: the reminders that are due, with one post for occurrences that start together, and notices when one that members were told about moves, is cancelled or is back on.

**Features** (in `features/`):

| Extension | What it does | Jobs |
|-----------|--------------|------|
| `kcpc.admin` | `/kcpc`: each server's feature settings (on/off, channel, ping role) and `/kcpc status`; the group that features attach their admin commands to | |
| `kcpc.workshops` | Reminders of the club's Luma workshops, `/event` | `workshops.sync` |
| `kcpc.contests` | Contest reminders from Codeforces (TLE's cache), AtCoder, icpc.global and clist.by, plus the club's own contests; results posts of members' rating changes after Codeforces contests (TLE's `RatingChangesUpdate` event, with catch-up from TLE's cache) and AtCoder contests (profile reads); `/contests` | `contests.sync.<source>`, `contests.results` |
| `kcpc.accounts` | Linking Codeforces and AtCoder accounts by a token on the profile, `/profile`, `/rank`; the source of AtCoder handles for other features | `accounts.refresh`, `accounts.purge-challenges` |
| `kcpc.problems` | `/randproblem` and the Friday weekly problem with its solution | `problems.refresh`, `weekly.post` |
| `kcpc.algo` | The algorithm of the month | `algo.post` |
| `kcpc.notify` | `/notify`: members take or drop a feature's ping role | |

### 11. Access control (`tle/access/`)

Every command, TLE's or KCPC's, prefix or slash, has a rule: who may use it, and where it answers publicly. One check applies the rule before every command runs, and the answers of a slash command used outside its place go only to the member who used it. Each server's admins choose its bot channels and staff channel, and can tighten any command's rule there with a limit (`/access`); README §3 describes the model for admins.

**The pieces.** Four modules are pure: they import nothing from Discord, so every decision can be tested on its own (`tests/unit/test_access_rules.py` checks the imports).
- `rules.py`: the model. `Who` is the members a command is for: everyone, trusted, moderator, developer, admin or owner. The levels are not a ladder: admins pass every level but the owner's, moderators also pass trusted, developers only their own, and the owner only the owner's. `Where` is where it answers publicly: anywhere, bot (the bot channels, which include the staff channel), bot-only, staff or staff-only; outside its place a slash command answers privately, and the `-only` places refuse it there instead. A `Rule` is a command's default, a `Limit` how a server tightened it, and `effective()` combines them into an `Effective` rule: every `who` must pass, the strictest `where` wins (`Where.tighten`), and `private` and `off` apply if any limit sets them. `decide(rule, asker, spot)` gives a `Decision` with one of seven outcomes: PUBLIC, PRIVATE, NOT_ALLOWED, OFF, BROKEN, WRONG_CHANNEL or PRIVATE_ONLY. `FAIL_CLOSED` is `Rule(OWNER, STAFF_ONLY)`.
- `table.py`: `RULES`, every command's default rule by qualified name (154 of them); `TWINS`, the four pairs of names for one command (`contests upcoming` is `contests`, the group's own command; `handle` is `handle show`); `PROTECTED_ROOTS` (`help`, `access`), which take no limits; `limit_keys(name)`, the keys of the limits that apply to a command (`name`, `name *`, then `group *` for each group above it); `OWNER_AND_ADMIN`, the bot owner's commands that also need the owner to be an admin (the club contest commands, which keep KCPC's admin check); `PRIVATE_ON_SLASH` and `BY_DIRECT_MESSAGE`, the commands whose slash answers are always private and those that answer by direct message, as /help says; and the categories of /help's pages.
- `settings.py`: `GuildAccess`, a server's bot channels (25 at most), staff channel and limits, and the versioned JSON it is stored as, one row per server in `user.db`'s `access_settings` table. Decoding fails closed: a row that can't be read is `broken`, a bad bot channel is dropped and a bad limit becomes `off`.
- `policy.py`: `effective_for(name, settings)`, a command's rule in one server; and `describe_who`, `describe_where` and `describe_limit`, the plain-English words that /help and /access share.

The rest connects them to Discord:
- `service.py`: `AccessService`, kept as `bot.access`. It holds every server's settings in memory (read once at start-up; each change is written to the database first, under a lock per server), finds the bot's owners once, and runs `check`, the bot-wide check of every command. `denial` turns a refusal into its text; `decide` answers for any command without refusing or keeping anything, for /help; `slash_path` and `listed_slash_path` give a command's slash form, if the tree still has it, and if the member's slash list shows it; `component_allowed` checks the buttons of commands' replies (who, off, broken settings and the allow-list, not channels). `AccessTree` is the bot's command tree.
- `context.py`: `TLEContext`, the context of every command. When the decision says a slash command answers privately, it makes every `send`, `defer` and `typing` private, and it never makes an answer public.
- `slash.py`: `apply_visibility`, which keeps staff commands out of members' slash lists.
- `help.py`: the Help cog (`/help`) and `send_help`, through which a group's own command shows its help (`TLEContext.send_help`).
- `cog.py`: the Access cog (`/access`).

**How a command is decided.** A prefix command goes from `process_commands` to `get_context`, which makes a `TLEContext`, to `invoke`, where discord.py runs the bot-wide checks. A slash command first meets `AccessTree.interaction_check`, which refuses private messages, servers outside `ALLOWED_GUILD_IDS` and application commands that no prefix command wraps; discord.py then runs the same checks for the hybrid command, with a context made from the interaction. Either way, `AccessService.check(ctx)`:
1. refuses private messages (`NoPrivateMessage`), and silently any server outside `ALLOWED_GUILD_IDS`;
2. works out the command's rule here with `effective_for`: the table's rule, or `FAIL_CLOSED`, with the server's limits under `limit_keys`. The bot owner's commands and the protected ones ignore limits; broken settings make every other command BROKEN; and until the server has a staff channel, or once the server no longer has the stored one, the `/access` commands work anywhere, so that an admin can set one;
3. reads the member (`asker`): Manage Server, TLE's roles as `constants` names them (admin, moderator and trusted by ID or name, developer by ID alone), read at call time, and whether they own the bot. The server's default role never counts as one of TLE's roles: every member has @everyone, whose ID is the server's;
4. reads the place (`spot`): the channel, a thread's parent for a thread, and the server's bot channels and staff channel;
5. decides (`rules.decide`): who first, so that a member who may not use a command learns nothing about it; then broken settings and `off`; then the place. A slash command outside its place answers privately, unless the place is `-only`; a prefix command outside its place is refused, and so is any prefix command whose answers must be private;
6. keeps the decision on the context (`cache_decision`) for `TLEContext`, and lets the command run, or raises `AccessDenied` with the text of `denial`, which `bot_error_handler` sends (§1).

Running the check again changes nothing: it has no side effects but the decision it keeps. An autocomplete gets suggestions only when the member may use the command where they are typing, and is never answered otherwise.

**Refusal texts** are constants in `service.py`. They never name a role or an ID, and never show the staff channel to a member who isn't staff (a slash refusal links it for staff; a prefix one, which the channel sees, doesn't). Admins are told the fix, such as "There is no bot channel yet. Add one with `/access bot-channels add`.", or the `;access` command for an admin whose slash list doesn't show `/access`. A prefix refusal for being outside the command's place adds "Or use `/x` here: only you will see the answer." when the place isn't `-only` and the member's slash list shows `/x`.

**Fail-closed rules.**
- A command without a rule is the bot owner's alone, in the staff channel (`FAIL_CLOSED`). `report_unruled` logs a WARNING for each at start-up, and `tests/kcpc/component/test_command_catalog.py` fails if any command, prefix or slash, lacks a rule, or any rule names no command.
- A server whose stored row can't be read is broken: every command but `/access`, `/help` and the bot owner's is refused until an admin uses `/access reset all`, which rewrites the row; its bot channels and staff channel, which couldn't be read either, must then be set again. If the table can't be read at all, every server is broken and no change is stored, so that a reset never replaces rows that were never read: a change first reads the table again (`ensure_readable`), and is refused with `SettingsUnreadable` if it still can't. The ERROR says which row to repair, and the refusals send members to the bot owner rather than to a reset. A stored limit that can't be read switches its command off.
- `TLEContext` counts a slash command without a kept decision as private, when the bot has an access service.
- A private answer that comes after the interaction expired raises `PrivateAnswerExpired`, which is logged and never sent, rather than posted in the channel (see the pitfalls below).
- `AccessTree` refuses application commands that no prefix command wraps ("This command isn't available."), and gives no suggestions for a command the member may not use.
- Buttons follow their command's rule, without its channels: the challenge's Accept, Decline and Withdraw (`duel accept`, `duel decline`, `duel withdraw`) and KCPC's Verify (`link verify`) call `component_allowed`, which answers a refusal privately itself. A button of a command without a rule is the owner's alone, with one WARNING per command.
- Under `--nodb` the settings live in memory (`persistent` is False), and `/access` says so.

**The bot's owners.** `resolve_owners()` reads `application_info()` once, at start-up: the application's owner, or the admins and developers of the team that owns it, as discord.py's `Bot.is_owner` counts them. `TLEBot.is_owner` asks the service and never Discord, so team changes take effect after a restart. If Discord can't be asked, one WARNING is logged, and an owner's command asks again, at most every 5 minutes. The owner level is neither implied by admin nor implies it.

**The slash pass.** Discord shows every member each synced slash command unless they lack the top-level command's default permissions (ignored on subcommands). So `apply_visibility`, run after every cog loads and before `tree.sync()`, goes through each top-level slash command by the rules of the commands in it: a tree of nothing but the bot owner's commands is removed (`/cache`); one whose code sets default permissions keeps them, with all its commands (`/kcpc`, `/access`); one of staff commands alone (`STAFF_LEVELS`) needs Manage Messages if they are all moderators', and Manage Server otherwise; from any other group, the staff commands are removed with `app_group.remove_command` (never the hybrid group's), then the subgroups left empty. A group's own rule counts through its slash fallback, whose `wrapped` is the group. Every prefix form stays, and server limits can't change the slash list. `tests/kcpc/component/test_slash_menu.py` pins the result, command by command.

**/help** lists the commands that the member can use where they ask, a page per category, with the form that works there. A command's category is the bot owner's for an owner rule, Server setup for `kcpc*` and `access*`, then by name for a few (`gudgitters`), and otherwise its cog's (`table.category_of`). It asks `decide` and `listed_slash_path`, never `can_run` (see the pitfalls). A command the member may not use gets the same answer as an unknown one. A command's help says what each form does in the channel: the commands in `PRIVATE_ON_SLASH` answer only the member on slash, whatever the channel, and those in `BY_DIRECT_MESSAGE` by direct message. Slash answers are private; a prefix answer is public, so it lists only commands for everyone and points to `/help x` for the rest. A group's help also lists the subcommands that work only elsewhere, marked "(bot channels only)" or, for staff and never in public, "(staff channel only)". Admins also see a command's default rule and the server's limits on it.

**/access** has `show` (`;access`), `bot-channels add|remove`, `staff-channel`, `limit` and `reset`. Every slash answer is private. Before there is a staff channel, or once it is deleted, its subcommands work in any channel, and bare `;access` only points to `/access show` and `/access staff-channel`, since its answer would be public. `limit` merges the options given into the command's limit, under the key `canonical(name)`, or the group's name and ` *` for a group with its subcommands; a slash fallback or a twin limits the group's own command alone unless asked otherwise. `reset all` uses `GuildAccess.without_limits()`, which also repairs broken settings. The cog's own `cog_check` admits admins alone, as a second line of defence.

**Tests.** Unit: `test_access_rules.py`, `test_access_settings.py`, `test_access_table.py` (it also reads the cogs' decorators, without importing them, against the table), `test_access_policy.py`. Component, on a real `commands.Bot`: `test_access_check.py`, `test_tle_context.py`, `test_access_tree.py`, `test_access_slash.py`, `test_help.py`, `test_access_cog.py` and `test_help_and_access.py`. On the booted bot: `test_command_catalog.py`, `test_slash_menu.py`, `test_bot_access.py` (an ordinary member's experience) and `test_command_texts.py` (the style guide below, and how commands answer).

---

## Data Flow Examples

### Command: `;gimme +dp 1400`
```
User Input -> Bot.process_commands -> get_context (TLEContext) -> invoke
  -> AccessService.check: everyone, bot channels; outside them a ;command is refused
  -> cooldown (once every 10 s for each member) -> Codeforces.gimme()
  -> Parse tags ["dp"] (from +dp), rating 1400
  -> cf.user.status(handle=...) to get submissions
  -> cf_common.cf_cache.problem_cache.problems (cached list)
  -> Filter by tag, rating, exclude solved
  -> Random selection from matching problems
  -> Create embed with problem link
  -> ctx.send(embed)
```

### Background: Rating Change Detection
```
RatingChangesCache._update_task fires periodically
  -> cf.contest.ratingChanges(contest_id=...)
  -> Store in cache_db (via aiosqlite)
  -> event_sys.dispatch(RatingChangesUpdate)
  -> Handles cog listener wakes up
  -> For each guild with auto_role_update enabled:
     -> Fetch new ratings, compare to old
     -> Update Discord roles to match new rank
     -> Post rank changes to configured channel
```

---

## Command text style guide

What members read about a command comes from its code: Discord shows its brief and its options' descriptions in the slash list, and /help shows its brief, help, usage, options and examples. `tests/kcpc/component/test_command_texts.py` checks the mechanical parts over the booted bot, and `tests/kcpc/component/test_feature_texts.py` checks more of KCPC's feature commands.

- **Language.** Plain technical English, British spelling, addressed to "you". No abbreviations such as cf or vc in prose; the names of commands, options and choices stay as they are.
- **Brief** (`brief=`), the slash description and the command's line in /help's lists: a verb phrase, capitalised, without a trailing period, at most 80 characters. "Get a problem to solve for gitgud points", never "Challenge" or "The next KCPC workshop".
  - A group whose slash fallback `show` shows the group's help: "Show the <topic> commands". Only such a group says so. `/kcpc` and `/access`, whose `show` shows the server's settings, say "Show this server's … settings".
  - A container group, without a fallback: a descriptive verb phrase, such as "Link, show and look up Codeforces handles".
  - A twin has the brief of the command it is a twin of.
- **Help** (the docstring, or `help=` where it must quote the cog's constants): one to three short sentences, then an `Examples:` block, with one invocation per line, indented, exactly as typed. How /help shows it:
  - the text before `Examples:` is the description, and the lines of each paragraph are joined;
  - lines starting with `-`, `*` or `•`, or set in, stay apart, as a list;
  - rows whose cells are separated by ` | ` go in a code block, as a table;
  - each example goes in a code span.
- **Examples** run the command they document, or one of its subcommands: its slash path (a group's fallback, such as `/clist show`, for the group's own command), or its `;` form. A `/` example must be in members' slash lists after the slash pass, so `;handle set`, not `/handle set`. Name options as Discord does (`/gitgud delta:200`), only options the command has. Discord fills a command's required options in order, so their values may come first without names (`/kcpc channel workshops #workshops`), but it takes an optional option's value only after its name: `/help command:clist future`, never `/help clist future`. No example line starts with a label, such as `member:`.
- **Options.** Every slash option is described, with `@app_commands.describe` or a flag's `description`, in at most 100 characters: discord.py cuts a longer description short, ending it with "…". An optional one says what happens without it, as in "The member whose duels to show; you if left out".
- **`usage=`.** None on hybrid commands: discord.py derives the usage from the signature, and /help spells a flags parameter out flag by flag. Written by hand on `;`-only commands, as `[handles...] [+tag...] [~tag...]`.
- **Never** put a mention in a code span (Discord shows it as text), name a role or an ID (texts say "a moderator or admin", since the roles can have any name; examples type `@alice` and `#general`), or rename a command or option.
- **Replies** go through `ctx.send`, so that `TLEContext` keeps a private answer private; `ctx.channel.send`, `interaction.channel.send`, `ctx.author.send` and `interaction.user.send` are allowed only in the places `test_command_texts.py` lists (the duel challenge's buttons, `;handle identify`'s direct message and `;meta guilds`). A cog's errors are its `CogError`s, answered privately on slash through `send_error_if`. No code calls `ctx.invoke` or `reinvoke`, which skip a command's checks, the access check included.
- **The slash list is pinned.** `test_slash_menu.py` lists every slash form; a change to one updates `SLASH_LIST` and explains why.

---

## discord.py pitfalls

These cost the access rules time; tests pin each one.

- **The fallback twin trap.** In a hybrid group with `fallback='upcoming'`, a prefix twin declared inside the group, `@group.command(name='upcoming', with_app_command=False)`, silently removes the fallback from the slash group when the cog is created: `Cog.__new__` re-parents subcommands with `HybridGroup.remove_command`, which also removes the app command of the same name, and points the fallback's `wrapped` at the twin. KCPC attaches such twins in `cog_load` instead (`self.contests.add_command(self.contests_upcoming)`); keep that pattern.
- **`can_run` answers the wrong question.** `Command.can_run(ctx)` runs every check with `ctx`'s channel and path, the bot-wide access check included: on /help's slash context it decides a prefix-only command as a slash command, which would answer privately anywhere, and it raises the refusal and keeps the decision on the context. To ask whether a member can use a command, call `AccessService.decide(command, member, channel, slash=...)`, which does neither, and `listed_slash_path` for the slash form the member sees, as /help does.
- **The expired-interaction fallback.** discord.py's `Context.send` posts in the channel, as an ordinary message, once a slash command's interaction has expired (15 minutes), dropping `ephemeral`. A private answer would then be public, so `TLEContext.send` raises `PrivateAnswerExpired` instead, which `bot_error_handler` only logs; error replies skip expired interactions altogether.
- **Removed slash forms still look attached.** `Group.remove_command` leaves the removed command's `parent`, and `tree.remove_command` leaves a hybrid command's `app_command`; only the tree shows that a form is gone. `slash_path` therefore looks each form up from the tree's root.
- **A group's checks don't guard its subcommands.** discord.py gives every hybrid group `invoke_without_command=True`, and then runs only a subcommand's own checks, never the group's: `;kcpc status` doesn't run the checks of `;kcpc`. So every command has a rule of its own, and KCPC puts its check on each admin command.
- **Cooldowns below the command decorator are shared.** `@commands.cooldown` applied to the function stores one `CooldownMapping`, which every copy of the command shares: one copy per cog instance, so one per bot in a test process. Above the command decorator, each copy gets its own (`test_a_cog_loaded_again_starts_without_cooldowns`). KCPC's link commands do this.
- **A cooldown counts a use before its arguments are parsed**, unless the command decorator passes `cooldown_after_parsing=True`; otherwise a mistyped command costs the wait, for `;ranklist` the whole server's. Every cooled-down TLE command that takes arguments passes it (`test_every_cooldown_of_tle_s_commands_counts_parsed_uses_only`), and a refusal that comes before any request to Codeforces gives the use back with `discord_common.undo_cooldown`.
- **Closed threads leave the cache.** discord.py forgets a thread once it is archived, so a channel ID stored for later posts can't be a thread's: `;remind here`, `/set_ratedvc_channel`, `/starboard here` and `/roleupdate publish here` refuse threads.
- **An optional channel on prefix.** With `channel: TextChannel | None = None`, a prefix command reads text that names no channel as "no channel". `/access staff-channel` uses `commands.parameter(converter=..., default=None)`, so such text is refused rather than clearing the staff channel.
- **`send_help` without a help command.** With `help_command=None`, discord.py's `Context.send_help` returns at once, sending nothing; `TLEContext.send_help` sends the help through `tle.access.help`.
- **An instance attribute hides the tree's check.** `bot.tree.interaction_check = ...` on the instance would hide `AccessTree.interaction_check`, where the allow-list, private messages and the autocomplete gate live.
- **Uncached threads.** A prefix command in a thread that discord.py hasn't cached comes with a `PartialMessageable` channel, without its parent, so the check can't count it as its parent channel and refuses the command. That fails closed; slash commands carry the thread with its parent.

---

## Configuration

| Source | Variables |
|--------|-----------|
| `.env` | `BOT_TOKEN`, `LOGGING_COG_CHANNEL_ID`, `ALLOW_DUEL_SELF_REGISTER` |
| `.env` (access) | `ALLOWED_GUILD_IDS` (the servers the bot may be used in, by ID; unset, any), read by `tle.config` |
| `.env` (roles) | `TLE_ADMIN`, `TLE_MODERATOR`, `TLE_TRUSTED`, `TLE_PURGATORY` (role IDs, recommended, or names), `TLE_DEVELOPER` (a role ID only; unset, no developer role), read by `tle.constants` |
| `.env` (OAuth) | `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_REDIRECT_URI`, `OAUTH_SERVER_PORT` (default 8080) |
| `.env` (KCPC) | `DISABLED_EXTENSIONS`, `KCPC_TIMEZONE`, `KCPC_DB_PATH`, `HTTP_USER_AGENT`, `LUMA_CALENDAR_ID`, `ICPC_CONTEST_CODES`, `CLIST_USERNAME`, `CLIST_API_KEY`, read by `tle.config` |
| Runtime | `--nodb` flag disables database (uses `DummyUserDbConn`); access settings are kept in memory |

---

## Docker Deployment

The Dockerfile uses a multi-stage build:

1. **Builder stage** (`python:3.11-slim`): Compiles native dependencies (cairo, PyGObject, PIL) with build tools
2. **Runtime stage** (`python:3.11-slim`): Slim image with only runtime libraries, CJK fonts, and compiled packages
3. Runs as non-root `botuser` for security
4. `docker-compose.yaml` defines a single service with `./data` volume mount, `.env` passthrough, and OAuth callback port exposure

---

## Known Architectural Limitations

1. **Global mutable singletons** - `user_db`, `cf_cache`, `event_sys`, `active_groups` live as module-level globals in `codeforces_common.py` (also attached to bot instance for cog access)
2. **No ORM or migration system** - Raw SQL with inline schema creation and migration code mixed into `create_tables()`
3. **In-memory state not persisted** - Duel draw offers, active command guards, and guild locks exist only in memory
