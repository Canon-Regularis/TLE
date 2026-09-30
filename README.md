# TLE ― The Competitive-Programming Discord Bot

TLE is a feature-packed Discord bot aimed at competitive programmers.
It can recommend problems, show stats & graphs, run duels on your server
and manage starboards – all with a single prefix `;`.

If you have Docker ≥ 24 (or Docker Desktop on Win/Mac) you are ready to
go.

---

## 1 · Features (quick glance)

| Cog | What it does |
|-----|--------------|
| **Codeforces** | problem / contest recommender, rating changes, user look-ups |
| **Contests** | shows upcoming & live contests |
| **Graphs** | rating distributions, solved-set histograms, etc. |
| **Handles** | link Discord users to CF handles |
| **Starboard** | pins popular messages to a channel |
| **CacheControl** | warm-up & manage local caches |

All graphs require cairo + pango; the Docker image already contains
everything.

### KCPC club features

The bot also runs the KCPC club's own features (in `tle/kcpc`): contest and
workshop reminders, account linking, a weekly problem and more, added in
phases. [KCPC_ARCHITECTURE.md](KCPC_ARCHITECTURE.md) explains the design and
the plan. KCPC keeps its data in its own database, `data/db/kcpc.db`, and
doesn't run when the bot is started with `--nodb`.

Every extension, TLE's or KCPC's, can be switched off with
`DISABLED_EXTENSIONS`: e.g. `tle.duel,tle.graphs,tle.starboard`, or `kcpc`
for all of KCPC. KCPC's own settings are all optional: `KCPC_TIMEZONE`,
`KCPC_DB_PATH`, `HTTP_USER_AGENT`, `LUMA_CALENDAR_ID`, `ICPC_CONTEST_CODES`,
`CLIST_USERNAME` and `CLIST_API_KEY` (see §3 and `.env.example`).

Server admins set KCPC up with `/kcpc` (or `;kcpc`). It needs the Manage
Server permission or the `TLE_ADMIN` role. Discord only shows `/kcpc` to
members with Manage Server, so give admins who only have the role access
under Server Settings → Integrations.

```text
/kcpc show                          every feature's settings in this server
/kcpc channel workshops #workshops  where a feature posts (checks the bot can)
/kcpc role workshops @Workshops     the role its posts mention (optional)
/kcpc enable workshops              turn it on (/kcpc disable turns it off)
/kcpc status                        database, jobs, post counts and recent skips
```

The features are `algo`, `contests`, `weekly` and `workshops`.

---

## 2 · Quick start (production)

```bash
# 1 · clone the repo
git clone https://github.com/cheran-senthil/TLE
cd TLE

# 2 · create a config file
cp .env.example .env          # then edit BOT_TOKEN, LOGGING_COG_CHANNEL_ID …

# 3 · create the data directory and start the bot (first run takes ~2 min)
mkdir -p data
docker compose up -d
```

That’s it.  
The bot will appear online in your Discord server; use
`;help` inside Discord to explore commands.

Compose restarts the bot after a crash or a reboot (`restart: unless-stopped`),
so `;meta kill` restarts it; stop it with `docker compose stop`.

### Updating to a new release

```sh
git pull
docker compose build --pull    # fetch newer base images
docker compose up -d           # recreate the container on the new image
```

---

## 3 · Environment variables ( `.env` )

| Variable | Required | Example | Description |
|----------|----------|---------|-------------|
| `BOT_TOKEN` | ✅ | `MTEz…` | Discord bot token from the Dev Portal |
| `LOGGING_COG_CHANNEL_ID` | ✅ | `123456789012345678` | channel where uncaught errors are sent |
| `ALLOW_DUEL_SELF_REGISTER` | ❌ | `true` | let users self-register for duels |
| `TLE_ADMIN` | ❌ | `Admin` | role name that can run admin cmds |
| `TLE_MODERATOR` | ❌ | `Moderator` | role name that can run mod cmds |
| `DISABLED_EXTENSIONS` | ❌ | `tle.duel,tle.graphs` | extensions, or families (`tle`, `kcpc`), to switch off |
| `KCPC_TIMEZONE` | ❌ | `Europe/London` | the club's time zone, for schedules and times admins type |
| `KCPC_DB_PATH` | ❌ | `data/db/kcpc.db` | where the KCPC database lives |
| `HTTP_USER_AGENT` | ❌ | `KCPC-bot (+https://…)` | User-Agent of KCPC's requests to other sites |
| `LUMA_CALENDAR_ID` | ❌ | `cal-…` | Luma calendar for servers that haven't set their own |
| `ICPC_CONTEST_CODES` | ❌ | `UKIEPC,Northwestern-Europe-2027` | icpc.global contests to track |
| `CLIST_USERNAME`, `CLIST_API_KEY` | ❌ | | clist.by account, an optional extra contest source |

Feel free to add any extra variables your cogs consume; Compose passes
every key in `.env` to the container. Run without Docker, the bot reads
`.env` itself; variables already set in the environment take precedence.

---

## 4 · Data folder

`docker compose` mounts `./data` into the container. It holds:

* `db/user.db`: TLE's server data, such as linked handles, duels, reminder
  settings and starboards.
* `db/kcpc.db`: each server's KCPC settings, the progress of KCPC's scheduled
  jobs, and the delivery ledger, which records KCPC's automatic posts so that
  none goes out twice. Before each upgrade of its database the bot copies it
  to `kcpc.db.v<N>.bak`, next to it. If you set `KCPC_DB_PATH`, these files
  are there instead; under Docker, keep that path inside `data/`, or they are
  lost when the container is recreated.
* `db/cache.db`: TLE's Codeforces cache. The bot refills most of it by itself,
  but an admin has to refill the rating changes and problemsets with
  `;cache ratingchanges all` and `;cache problemsets all`.
* `misc/contest_writers.json`: an optional list of contest writers, made with
  `extra/scrape_cf_contest_writers.py`.
* `temp/`: images the bot is drawing.

Only `db/cache.db` (then refill it as above) and `temp/` are safe to delete.
Keep the rest and back it up: losing `kcpc.db` loses every server's KCPC setup
and the record of what was posted, so reminders could go out again.

To back up the databases, stop the bot (`docker compose stop`) and copy
`data/db`, or use sqlite3's `.backup` command while it runs. The databases use
WAL mode, so while the bot runs, recent writes can still be in the `-wal` file
next to each one, and a copy of the database file alone can miss them.

---

## 5 · Local development (optional)

You can hack on the code without touching your system Python:

```bash
# live-reload dev run (blocks & shows logs)
docker compose up --build
```

Lint & format (Ruff):

```bash
docker run --rm -v $PWD:/app -w /app python:3.11-slim \
       sh -c "pip install ruff && ruff check . && ruff format --check ."
```

---

## 6 · Repository layout

```sh
.
├─ Dockerfile              # 2-stage image, installs native cairo stack
├─ compose.yaml            # single-service compose file
├─ requirements.txt        # runtime Python deps (no pins)
├─ .env.example            # template for your secrets
├─ data/                   # databases & caches, see §4 (git-ignored)
├─ tle/ …                  # bot source code
└─ extra/ fonts.conf …     # helper resources
```

---

## 7 · Contributing

Pull requests are welcome!  
Before opening a PR, please

1. run `ruff check --fix .` (auto-formats touched lines),
2. keep commits focused; large refactors in a separate PR.

---

## 8 · License

MIT ― see `LICENSE`.
