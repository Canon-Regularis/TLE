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

The bot also runs the KCPC club's own features (in `tle/kcpc`), added in
phases. So far it reminds members of the club's workshops on Luma and of
contests: Codeforces, AtCoder, ICPC and the club's own, and with a clist.by
account, CodeChef, LeetCode, TopCoder and the ICPC World Finals. Account
linking and a weekly problem are to follow.
`tle/kcpc/__init__.py` outlines how it is put together. KCPC keeps its data
in its own database, `data/db/kcpc.db`, and doesn't run when the bot is
started with `--nodb`.

Every extension, TLE's or KCPC's, can be switched off with
`DISABLED_EXTENSIONS`: e.g. `tle.duel,tle.graphs,tle.starboard`, or `kcpc`
for all of KCPC. KCPC's extensions are `kcpc.admin` (`/kcpc`),
`kcpc.workshops` (workshop reminders, `/event` and `/kcpc workshops`),
`kcpc.contests` (contest reminders, `/contests` and `/kcpc contests`) and
`kcpc.notify` (`/notify`). KCPC's own settings are all optional:
`KCPC_TIMEZONE`, `KCPC_DB_PATH`, `HTTP_USER_AGENT`, `LUMA_CALENDAR_ID`,
`ICPC_CONTEST_CODES`, `CLIST_USERNAME` and `CLIST_API_KEY` (see §3 and
`.env.example`).

Server admins set KCPC up with `/kcpc` (or `;kcpc`). It needs the Manage
Server permission or the `TLE_ADMIN` role. Discord only shows `/kcpc` to
members with Manage Server, so give admins who only have the role access
under Server Settings → Integrations.

```text
/kcpc show                          every feature's settings in this server
/kcpc channel workshops #workshops  where a feature posts (checks the bot can)
/kcpc role workshops @Workshops     a pings-only role its posts mention (optional)
/kcpc enable workshops              turn it on (/kcpc disable turns it off)
/kcpc status                        database, jobs, post counts and recent skips
```

The features are `algo`, `contests`, `weekly` and `workshops`; so far only
`contests` and `workshops` post anything.

#### Workshop reminders

To have the bot remind a server of the club's workshops:

```text
/kcpc channel workshops #workshops          where the reminders go
/kcpc role workshops @Workshops             a pings-only role they mention
/kcpc workshops calendar <ID or iCal link>  the club's Luma calendar
/kcpc enable workshops                      start reminding
```

The calendar is its ID (`cal-…`) or its iCal link, from "Add to calendar" on
the calendar's Luma page. A server that sets none follows `LUMA_CALENDAR_ID`.
The bot reads the calendar every 10 minutes (`/kcpc workshops sync` reads it
at once) and reminds members 24 hours and 1 hour before each workshop. If a
workshop they were reminded of moves, is cancelled or comes back, it tells
them. Luma drops cancelled events from the calendar, so a workshop counts as
cancelled once three reads in a row have missed it, the last at least 20
minutes after it was last listed (about half an hour; `/kcpc workshops sync`
can't hurry this). If more than half of four or more upcoming workshops vanish
at once, as a glitch at Luma would look, the bot still applies the rest of the
calendar at once, but counts the missing ones as cancelled only after six reads
over at least 50 minutes (about an hour).

Members can use:

```text
/event next                the next workshop
/event this-week           this week's workshops, Monday to Sunday
/notify workshops on|off   get the workshop pings, or stop them
```

`/notify` gives members the feature's ping role, or takes it away. For that
the bot needs the Manage Roles permission, and its highest role must be above
the ping role (Server Settings → Roles). As any member can have it, the ping
role must be just for pings, and `/notify` refuses any other: a role with a
permission that @everyone lacks, one that changes what members can do in a
channel (such as a member or verified role), or one of TLE's roles (admin,
moderator, trusted or purgatory).

#### Contest reminders

To have the bot remind a server of upcoming contests:

```text
/kcpc channel contests #contests                 where the reminders go
/kcpc role contests @Contests                    a pings-only role they mention
/kcpc contests platforms codeforces atcoder      which platforms (all by default)
/kcpc contests start-posts on                    also post as each contest starts
/kcpc enable contests                            start reminding
```

The platforms are `codeforces`, `atcoder`, `codechef`, `leetcode`,
`topcoder`, `icpc` and `manual` (the club's own contests, see below); a server
follows all of them until it picks some with `/kcpc contests platforms`. The
bot reads Codeforces every 5 minutes (from TLE's own copy of its contest
list), AtCoder every 30 minutes, icpc.global every 6 hours and clist.by (see
below) every 30 minutes; `/kcpc contests sync` reads them all at once. It
reminds members an hour before each contest, with one message for contests
that start together, such as a Div. 1 and a Div. 2 round, and with start posts
on, it posts again as they start. If a contest they were reminded of moves, is
cancelled or comes back, it tells them. A Codeforces, AtCoder, CodeChef,
LeetCode or TopCoder contest that drops off its site's list before it starts
counts as cancelled once three reads in a row have missed it, the last at least
20 minutes after it was last listed. One that has started never does: AtCoder
drops contests from its list as they start. ICPC contests, the regionals and
the World Finals, are never cancelled this way, because the bot reads only some
ICPC contests; an admin can still move one with `/kcpc contests settime`.

CodeChef, LeetCode, TopCoder and the ICPC World Finals come from clist.by,
which lists the contests of many sites, once `CLIST_USERNAME` and
`CLIST_API_KEY` are set (see §3). To get a key, sign up for a free account at
[clist.by](https://clist.by), then open its API documentation page,
<https://clist.by/api/v4/doc/>, which shows your username and API key. The
World Finals count as `icpc` contests. Without the key, the `codechef`,
`leetcode` and `topcoder` platforms have no contests; admins can narrow a
server's platforms with `/kcpc contests platforms`.

ICPC contests come from icpc.global: those in `ICPC_CONTEST_CODES` (see §3).
icpc.global gives only the dates of their events, so each is listed by its
event's first day, as "time TBA", and gets no reminders until an admin sets
the contest's own time with `/kcpc contests settime`. Until then, a contest
whose event lasts several days, such as NWERC (27-29 November 2026), drops out
of `/contests upcoming` from that first day. Admins can also add the club's
own contests:

```text
/kcpc contests settime <contest> <start> [duration]  set a contest's time
/kcpc contests add <name> <start> <duration> [url]   add a club contest
/kcpc contests remove <contest>                      remove a contest added with add
```

A start is in the club's time zone (`KCPC_TIMEZONE`), as
`YYYY-MM-DD HH:MM` (in quotes with `;kcpc contests add`), and a duration is
like `2h`, `90m` or `1h30m`. As you type the contest, `settime` suggests
upcoming contests. It works for any contest that isn't cancelled, and the time
it sets stays, whatever the site says later (if the site later moves the
contest away from that time, the bot posts a warning to the log channel,
`LOGGING_COG_CHANNEL_ID`, and `settime` can change the time again); without a
duration the contest keeps the one it has. If a contest members were reminded
of moves or is removed, they are told.

KCPC is built for one club, so all the servers the bot is in share its
contests. A club contest, or a time set with `settime`, reaches every server
that follows the contest's platform (`manual` for club contests, which servers
follow unless they pick platforms without it). So an admin of any server the
bot is in can add, retime or remove contests in all of them. Keep the bot in
the club's own servers only (in the Discord Developer Portal, under Bot, turn
off Public Bot), and try these commands with a separate test bot.

Members can use:

```text
/contests upcoming [platform]   the next 10 contests (also plain ;contests)
/contests live                  the contests running now
/notify contests on|off         get the contest pings, or stop them
```

TLE's own contest reminders (`;remind`) also ping for Codeforces rounds. In a
server that uses KCPC's contest reminders, a TLE admin should switch TLE's off
with `;remind clear`, or members are pinged twice for each round.

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
| `LUMA_CALENDAR_ID` | ❌ | `cal-…` | default Luma calendar (its ID), for servers that haven't set one |
| `ICPC_CONTEST_CODES` | ❌ | `UKIEPC,Northwestern-Europe-2027` | icpc.global contests to track, by abbreviation; icpc.global gives their dates, admins set their times (`/kcpc contests settime`) |
| `CLIST_USERNAME`, `CLIST_API_KEY` | ❌ | | clist.by username and API key; with both set, the bot also tracks CodeChef, LeetCode, TopCoder and the ICPC World Finals (see §1) |

Feel free to add any extra variables your cogs consume; Compose passes
every key in `.env` to the container. Run without Docker, the bot reads
`.env` itself; variables already set in the environment take precedence.

---

## 4 · Data folder

`docker compose` mounts `./data` into the container. It holds:

* `db/user.db`: TLE's server data, such as linked handles, duels, reminder
  settings and starboards.
* `db/kcpc.db`: each server's KCPC settings, the workshops and contests the
  bot has read (and the contests and times admins have set), the progress of
  KCPC's scheduled jobs, and the delivery ledger, which records
  KCPC's automatic posts so that none goes out twice. Before each upgrade of
  its database the bot copies it to `kcpc.db.v<N>.bak`, next to it. If you set
  `KCPC_DB_PATH`, these files are there instead; under Docker, keep that path
  inside `data/`, or they are lost when the container is recreated.
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
