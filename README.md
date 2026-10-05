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
account, CodeChef, LeetCode, TopCoder and the ICPC World Finals. After
Codeforces and AtCoder contests, it posts how members' ratings changed.
Members can also link their Codeforces and AtCoder accounts, for profiles and
server leaderboards. Each Friday it posts a problem from Codeforces or
AtCoder, with its solution the Friday after, and `/randproblem` gives members
a random problem by topic and difficulty. On the 1st of each month it posts
an algorithm of the month: a data structure or algorithm to learn, with where
to read about it.
`tle/kcpc/__init__.py` outlines how it is put together. KCPC keeps its data
in its own database, `data/db/kcpc.db`, and doesn't run when the bot is
started with `--nodb`.

Every extension, TLE's or KCPC's, can be switched off with
`DISABLED_EXTENSIONS`: e.g. `tle.duel,tle.graphs,tle.starboard`, or `kcpc`
for all of KCPC. KCPC's extensions are `kcpc.admin` (`/kcpc`),
`kcpc.workshops` (workshop reminders, `/event` and `/kcpc workshops`),
`kcpc.contests` (contest reminders and results, `/contests` and
`/kcpc contests`), `kcpc.accounts` (account linking, `/link`, `/unlink`,
`/profile`, `/rank` and `/kcpc accounts`), `kcpc.problems` (the weekly
problem, `/randproblem`, `/weekly` and `/kcpc weekly`), `kcpc.algo` (the
algorithm of the month, `/algo` and `/kcpc algo`) and `kcpc.notify`
(`/notify`). KCPC's own settings are all optional: `KCPC_TIMEZONE`,
`KCPC_DB_PATH`, `HTTP_USER_AGENT`, `LUMA_CALENDAR_ID`, `ICPC_CONTEST_CODES`,
`CLIST_USERNAME` and `CLIST_API_KEY` (see §3 and `.env.example`).

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

The features are `algo`, `contests`, `weekly` and `workshops`.

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
/kcpc contests results off                       no results posts (on by default)
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
server's platforms with `/kcpc contests platforms`. CodeChef's events that
aren't contests, such as its Placement Prep Weekends, are left out.

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

After each Codeforces and AtCoder contest of the server's platforms, the bot
posts the results in the same channel, without a ping: each member who took
part, with their handle and how their rating changed (old → new, and by how
much), the biggest gains first, and on Codeforces their place and any change
of rank, such as Pupil → Specialist. It lists members who linked the account
(`/link`, or TLE's `;handle set` for Codeforces) and are still in the server,
up to 30 of them (fewer if their lines are long), then how many more there
are, and a contest in which no member was rated gets no post.

Codeforces results are posted as soon as TLE has read the contest's rating
changes: usually a few hours after the contest, or the next day after a round
with a 12-hour open hacking phase (Educational, Div. 3 and Div. 4 rounds). The
bot reads them from TLE's own copy, so it asks Codeforces for nothing more,
and after a restart it catches up on the contests of the last 48 hours.
AtCoder lets bots read only users' profiles: their rating and how many rated
contests they have taken part in. So in the last half hour of an ABC, ARC or
AGC, the bot reads the profile of each member's linked AtCoder account, then
reads them again from 15 minutes after the end, every 30 minutes for up to 6
hours, until their ratings change. If the bot is down from before it sees
them change until those 6 hours run out, the contest gets no post. AtCoder
results have no places, as AtCoder's standings are off limits to bots.
Heuristic contests (AHC) and other series get no results posts.

While the bot reads for one AtCoder contest, another may end. A member's
rating change then goes to the contest that ended first, unless the member's
profile, read for the later contest over an hour after the first ended,
showed that they hadn't taken part in the first. So a member's change can go
to the wrong contest when two contests end together, when AtCoder rates both
between two of the bot's reads, when it rates the first over an hour after
its end, or when a contest ends less than an hour and a half after one that
no member took part in.

On a new install, the bot posts no results of contests that ended before it
started. Admins turn the results posts off, or on again, with
`/kcpc contests results off|on`, for Codeforces and AtCoder alike.

TLE's own contest reminders (`;remind`) also ping for Codeforces rounds. In a
server that uses KCPC's contest reminders, a TLE admin should switch TLE's off
with `;remind clear`, or members are pinged twice for each round. Likewise,
TLE's `;roleupdate publish here` posts the rank changes and the top rating
gains of a server's members after each rated Codeforces round, in the channel
where it was run. A server that wants one post per round should turn off one
of them: TLE's with `;roleupdate publish off`, or KCPC's with
`/kcpc contests results off`, which stops its AtCoder results too.

#### Account linking

Members link their Codeforces and AtCoder accounts to show their ratings and
compare them within the server:

```text
/link codeforces <handle>   link your Codeforces account
/link atcoder <handle>      link your AtCoder account
/link verify <platform>     finish linking (the same as the Verify button)
/unlink atcoder             unlink your AtCoder account
/profile [member]           linked accounts, with rating, peak and rank
/rank [platform]            the server's leaderboard (Codeforces by default)
/handle show [member]       a member's linked handles
```

A member proves that an account is theirs with a token. `/link` replies, so
that only they see it, with a token such as `kcpc-1a2b3c4d5e` and the steps: put
it in the account's Affiliation on AtCoder (<https://atcoder.jp/settings>), or
its Organization on Codeforces (<https://codeforces.com/settings/social>),
save, then press Verify under the reply (or use `/link verify`). The token
lasts 10 minutes, and `/link` again gives a new one. Once the account is
linked, remove the token from the profile again. A handle can be linked to
only one member of a server.

Links stay when a member leaves the server, for if they come back. An AtCoder
account that a member who left had linked is free, though: whoever proves it
is theirs takes the link over. Admins (Manage Server or the `TLE_ADMIN` role)
can unlink anyone's AtCoder account, e.g. one that a member linked but isn't
theirs, so that its owner can link it:

```text
/kcpc accounts unlink <handle>   unlink an AtCoder account, whoever linked it
```

It needs the `kcpc.admin` extension. Without it, only the member who linked an
AtCoder account can unlink it, with `/unlink atcoder`.

`/profile` and `/rank` show the ratings the bot last read. Every 6 hours it
reads the ratings of the accounts its servers' members have linked, and
`/profile` reads a member's again if they are more than an hour old (if the
site doesn't answer, it shows the older ratings and says so). `/rank` lists
the server's linked members by rating, unrated last, 10 to a page, and shows
where you stand.

A Codeforces account linked with `/link codeforces` is shared with TLE: it
sets the handle that `;handle set` sets, which TLE's own features, such as
gitgud, duels and the rank roles, use. The member gets the role for their
rank, so the server needs TLE's rank roles, and the bot needs the Manage Roles
permission with its highest role above them. If the role for a rated
account's rank is missing, `/link codeforces` says so before giving a token. A
member who already has a Codeforces handle can't link another, and
`/unlink codeforces` can't remove it: admins and moderators (`TLE_ADMIN`,
`TLE_MODERATOR`) change it with `/handle set` and remove it with
`/handle remove`. TLE keeps the handle of a member who leaves, for if they come
back, so nobody else can link it until it is removed. Where Codeforces OAuth is
set up, TLE's `;handle identify` links a Codeforces account too. AtCoder links
are KCPC's own, and TLE's features don't use them.

#### Weekly problem and /randproblem

To have the bot post a weekly problem in a server:

```text
/kcpc channel weekly #weekly-problem   where the problems and solutions go
/kcpc role weekly @Weekly              a pings-only role each problem mentions
/kcpc enable weekly                    start posting
```

`/randproblem` needs none of this: it works in every server where the
`kcpc.problems` extension is loaded, whether or not the weekly problem is on.

Every Friday at 12:00 in the club's time zone (`KCPC_TIMEZONE`), the bot posts
the solution of last week's problem, then a new problem; only the problem
mentions the role. The first problem comes the next Friday, unless an admin
posts this week's at once with `/kcpc weekly post-now`: the first time the bot
starts with this feature, it posts nothing, even on a Friday afternoon. If the
bot is down at noon, it posts when it is back, up to 6 hours late; any later
and it skips that week, though `post-now` can still post it until the next
Friday.

A week's problem is the oldest in the server's queue, or else a random one as
the server's rotation says: a cycle of entries, one a week, each a platform, a
band and a topic. The bands go by Codeforces' ratings: easy is below 1200,
medium 1200 to 1599, hard 1600 to 1999 and expert 2000 and up. AtCoder's
difficulties (AtCoder Problems' estimates) are converted to Codeforces' scale,
so an AtCoder 1376 counts as a Codeforces 1752. The default rotation is
Codeforces easy, AtCoder medium, Codeforces medium, AtCoder hard, all on any
topic. Weeks take a rotation's entries in turn by the calendar, so a week
without a problem doesn't shift the cycle. If the entry's platform has nothing
to give, the problem comes from the other platform, in the same band on any
topic. A server never gets the same problem twice.

The bot reads the lists of problems from Codeforces' API (through TLE's
client) every 6 hours and from AtCoder Problems (kenkoooo.com) every day.
Random picks, for the weekly problem and `/randproblem`, take Codeforces'
rated problems of standard rounds, leaving out April Fools, Kotlin Heroes and
other special contests as TLE does, and AtCoder's ABC, ARC and AGC problems
that AtCoder Problems gives a difficulty that isn't experimental: ABC from 042
and ARC from 058 on, and every AGC. The rotation takes Codeforces
problems only from contest 1000 on (mid-2018), as nearly all of those have an
English editorial.

The bot reads nothing from Codeforces but its API, which can't find a
contest's editorial, so a Codeforces problem's solution post gives the link an
admin set, or else the contest's page, where Codeforces lists the editorial
under "Contest materials". On AtCoder, the bot finds the editorials itself, on
atcoder.jp: the rotation picks only problems with an official editorial, and
the solution post links the best one (English text first, or an admin's link
instead) and the task's page of all its editorials.

Admins can choose the problems and see what comes next:

```text
/kcpc weekly queue <problem> [solution]   queue a problem, and its solution link
/kcpc weekly unqueue <problem>            take a problem out of the queue
/kcpc weekly solution <url> [week]        set a problem's solution link
/kcpc weekly rotation [entries]           show the rotation, or set it
/kcpc weekly preview                      what the next Friday will post
/kcpc weekly post-now                     post this week's problem now
```

A problem is a Codeforces problem, as `1520D` or its link, or an AtCoder one,
as `abc300_d` or its link. The queue holds up to 25 problems, which go out
oldest first, and refuses one the server has had, unless its post never went
out and its week is over. For an AtCoder problem queued without a link, the
bot looks up its editorials and says which one the solution post will link. A
solution link is a full http(s) URL of up to 300 characters. `solution` sets
the link of the problem for the Friday `week` (`YYYY-MM-DD`, as
`/weekly history` shows it), or without `week`, of the latest problem whose
solution hasn't been posted. A posted solution's link can't change, and a problem whose
post never went out, or more than 4 weeks old by the next post, gets no
solution post, so it takes no link.

A rotation has 1 to 52 entries, separated by commas or semicolons, each a
platform (`cf` or `codeforces`, `ac` or `atcoder`), a band and a topic (`any`
if left out), as in `cf easy, ac medium, cf medium graphs, ac hard`. Topics
are those of `/randproblem` (below); an AtCoder entry's can only be `any`.
`/kcpc weekly rotation default` goes back to the default rotation, and
without entries the command shows the rotation, marking the entry of the next
post. `preview` shows when and where the next problem posts (or why it won't),
what it will be (the queued problem, or the rotation's entry), this week's
problem and where its solution link comes from, the queue and the rotation.
`post-now` posts any solution that is due and this week's problem, or says
that they are out already, or that Discord refused this week's problem
earlier, so that it can't go out again that week.

Members can use:

```text
/randproblem <topic> <difficulty> [platform]  a random problem
/weekly current                               the latest weekly problem
/weekly history                               the weekly problems so far
/notify weekly on|off                         get the weekly pings, or stop them
```

`/weekly current` (also plain `;weekly`) and `/weekly history` (newest first)
link each problem's solution once it is out. `/randproblem` picks from
Codeforces unless `platform` is `atcoder`. Its topic is `any`, a Codeforces
tag such as `dp` or `greedy`, or a group of tags: `graphs`, `math`,
`number-theory`, `strings`, `data-structures`, `searching`, `brute-force` or
`constructive`. Its difficulty is a band or a rating from 800 to 3500, on
Codeforces' scale on both platforms. Both are suggested as you type (with
`;randproblem`, put a topic of several words in quotes). A rating takes the
problems rated at or near it, looking up to 200 away if none is closer, and
the reply says when it had to. The reply hides the problem's tags behind a
spoiler.

AtCoder's problems have no topics, so with `platform: atcoder` the topic must
be `any`. For a member with linked accounts, `/randproblem` leaves out the
problems they have solved: on Codeforces by the handle TLE has for them (which
`/link codeforces` sets), and on AtCoder by the account linked with
`/link atcoder`. It checks for up to 10 seconds; if a site is slow or down, it
gives a problem anyway and says it couldn't check them all. Until the bot has
read the problems after a restart, `/randproblem` and `/kcpc weekly queue` ask
to try again in a few minutes.

#### Algorithm of the month

To have the bot post an algorithm of the month in a server:

```text
/kcpc channel algo #algorithms   where the topics go
/kcpc role algo @Algo            a pings-only role each topic mentions
/kcpc enable algo                start posting
```

On the 1st of each month at 12:00 in the club's time zone (`KCPC_TIMEZONE`),
the bot posts a data structure or algorithm to learn that month, such as
prefix sums, Dijkstra's algorithm or the segment tree: what it is for, how far
into the syllabus it is (beginner, intermediate or advanced), and links to its
articles on GeeksforGeeks and, where it has one, cp-algorithms. The post
mentions the role. The first topic comes on the next 1st, unless an admin
posts this month's at once with `/kcpc algo post-now`: the first time the bot
starts with this feature, it posts nothing, even on the 1st. If the bot is
down at noon, it posts when it is back, up to 24 hours late; any later and it
skips that month, though `post-now` can still post it until the next 1st.

The topic is picked at random from a list of about 40, the usual
competitive-programming syllabus from prefix sums to maximum flow. A server
doesn't get a topic again until it has had every one in the list; then the
list starts over. A topic whose post never went out doesn't count.

Admins can change the topic and see what comes next:

```text
/kcpc algo reroll     replace this month's topic with another, and post it
/kcpc algo post-now   post this month's topic now
/kcpc algo preview    the next post, this month's topic and the topics left
```

`reroll` picks another topic that the server hasn't had since the list last
started over, and posts it, saying which topic it replaces, if that one was
posted; the topic it replaces can come up again later. When this month's
topic is the last one left before the list starts over, there is none to
reroll to. While Discord hasn't confirmed this month's last post, `reroll`
asks you to try again in a few minutes. `post-now` posts this month's topic,
or says that it is out already, or that Discord refused it earlier, so that
it can't go out again (`reroll` posts another). Before noon on the 1st, both
act on the month before, and their replies name the month. `preview` shows
when and where the next topic posts (or why it won't), this month's topic
and whether it was posted, and how many topics are left before the list
starts over.

Members can use:

```text
/algo current         this month's topic
/algo history         the topics so far
/notify algo on|off   get the algorithm of the month pings, or stop them
```

`/algo current` (also plain `;algo`) links the topic's articles, and
`/algo history` lists this month's and earlier months' topics, newest first.

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

* `db/user.db`: TLE's server data, such as members' Codeforces handles (those
  linked with `/link codeforces` too), duels, reminder settings and
  starboards.
* `db/kcpc.db`: each server's KCPC settings, the workshops and contests the
  bot has read (and the contests and times admins have set), members' AtCoder
  links and the links waiting to be verified, the ratings last read for
  `/profile` and `/rank`, each server's weekly problems and queue and its
  algorithms of the month, when results posts started and the contests whose
  results the bot has worked on since (members' rating changes, and the
  AtCoder ratings it compares them with),
  the progress of KCPC's scheduled jobs, and the delivery ledger, which
  records KCPC's automatic posts so that none goes out twice. Before each
  upgrade of its database the bot copies it to `kcpc.db.v<N>.bak`, next to
  it. If you set `KCPC_DB_PATH`, these files are there instead; under Docker,
  keep that path inside `data/`, or they are lost when the container is
  recreated.
* `db/cache.db`: TLE's Codeforces cache. The bot refills most of it by itself,
  but an admin has to refill the rating changes and problemsets with
  `;cache ratingchanges all` and `;cache problemsets all`.
* `misc/contest_writers.json`: an optional list of contest writers, made with
  `extra/scrape_cf_contest_writers.py`.
* `temp/`: images the bot is drawing.

Only `db/cache.db` (then refill it as above) and `temp/` are safe to delete.
Keep the rest and back it up: losing `kcpc.db` loses every server's KCPC
setup, members' AtCoder links and the record of what was posted, so reminders
could go out again.

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
