# TLE ― The Competitive-Programming Discord Bot

TLE is a feature-packed Discord bot aimed at competitive programmers.
It can recommend problems, show stats & graphs, run duels on your server
and manage starboards, with slash commands and the `;` prefix. Each
server's admins choose where its commands answer publicly, and can limit
who may use each one (see §3).

If you have Docker ≥ 24 (or Docker Desktop on Win/Mac) you are ready to
go.

---

## 1 · Features (quick glance)

| Cog | What it does |
|-----|--------------|
| **Codeforces** | problem / contest recommender, rating changes, user look-ups |
| **Contests** | shows upcoming & live contests |
| **Dueling** | duels between members on Codeforces problems, with duel ratings |
| **Graphs** | rating distributions, solved-set histograms, etc. |
| **Handles** | link Discord users to Codeforces handles, rank roles |
| **Meta** | how the bot is doing; the bot owner's restart and server list |
| **Starboard** | reposts popular messages in a channel |
| **CacheControl** | the bot owner's commands that reload the Codeforces caches |
| **Help**, **Access** | `/help` for everyone, and `/access` for admins (see §3) |

All graphs require cairo + pango; the Docker image already contains
everything.

### Notes on TLE's commands

- **Cooldowns.** Heavy commands, which ask Codeforces for a lot or draw plots,
  can be used only so often by each member: `/gitgud`, `/upsolve`, `;gimme`
  and `/duel complete` once every 10 seconds; `;stalk`, `;mashup`, `;vc`,
  `;fullsolve`, `;teamrate`, `;vcrating`, `;duel challenge`, `;duel rating`,
  `/gudgitters` and each plot once every 20 seconds; `;ratedvc` once a minute. `;ranklist`
  works once every 30 seconds in each server. Used too soon, a command says
  when it works again, and `/help` shows each command's cooldown. A mistyped
  command doesn't count, nor does one refused before it asks Codeforces
  anything, such as `/gitgud` while you have a challenge, `;ratedvc` outside
  its channel or `;ranklist` of a contest the bot doesn't know.
- **Ping roles.** `/role` gives or takes the roles named **Duelist** and
  **Virtual Contestant**. Create them just for pings: no permission beyond
  @everyone's, below the bot's highest role, and no channel overwrites, or
  `/role` refuses them. The same goes for the contest reminder role that
  `;remind here` sets: it refuses any other, and `/remind on` refuses a
  stored role that has become unsuitable. `/remind off` refuses only a role
  whose removal could raise a member's rights, such as one of TLE's roles or
  one that a channel denies permissions. Reminders ping that role and nobody
  else.
- **The trusted role** is the one `TLE_TRUSTED` names (by ID or name).
  `/handle refer`, `;handle grandfather` and a handle rated 1900 or more
  before 11 September 2024 give it. Rank roles are named after Codeforces
  ranks, such as Expert.
- **Rated virtual contests.** `;ratedvc` works only in its channel, which
  must be a bot channel that members can read, so not the staff channel;
  `/set_ratedvc_channel` and `/access show` warn when it isn't.
- **Duels.** `/duel recent` and `/duel ongoing` show only duels between
  members of this server. The Accept, Decline and Withdraw buttons under a
  challenge follow who may use `duel accept`, `duel decline` and
  `duel withdraw`, and whether this server switched them off, but not
  channels. `/duel selfregister` needs a Codeforces handle linked first, with
  `/link codeforces`.
- **Starboard.** Messages in the staff channel and its threads, in private
  threads, and in channels that some readers of the starboard channel can't
  see are never reposted.
- **The bot owner's commands.** `;meta guilds` sends the list of the bot's
  servers by direct message, never to a channel; `;meta kill` stops the bot;
  `;cache …` reloads the Codeforces caches that every server shares.

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
`CLIST_USERNAME` and `CLIST_API_KEY` (see §5 and `.env.example`).

Server admins set KCPC up with `/kcpc` (or `;kcpc`). It needs the Manage
Server permission or the `TLE_ADMIN` role. Discord only shows `/kcpc` to
members with Manage Server, so give admins who only have the role access
under Server Settings → Integrations (see §3). Like most staff commands,
`;kcpc` works in the staff channel only. `/kcpc` answers only you, wherever
you use it.

```text
/kcpc show                            every feature's settings in this server
/kcpc channel workshops #workshops    where a feature posts (checks the bot can)
/kcpc role workshops role:@Workshops  a pings-only role its posts mention (optional)
/kcpc enable workshops                turn it on (/kcpc disable turns it off)
/kcpc status                          post counts and recent skips (the bot owner also sees jobs)
```

`/kcpc status` is for developers (`TLE_DEVELOPER`) as well as admins, and
works in the staff channel alone: developers without Manage Server use
`;kcpc status` there, or `/kcpc status` once an admin shows `/kcpc` to the
developer role (Server Settings → Integrations, see §3). Only the bot owner
sees KCPC's extensions, jobs and their last errors, on `/kcpc status`, which
answers privately.

The features are `algo`, `contests`, `weekly` and `workshops`.

#### Workshop reminders

To have the bot remind a server of the club's workshops:

```text
/kcpc channel workshops #workshops          where the reminders go
/kcpc role workshops role:@Workshops        a pings-only role they mention
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
moderator, trusted, purgatory or developer).

#### Contest reminders

To have the bot remind a server of upcoming contests:

```text
/kcpc channel contests #contests                 where the reminders go
/kcpc role contests role:@Contests               a pings-only role they mention
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
below) every 30 minutes; the bot owner's `/kcpc contests sync` reads them all
at once. It reminds members an hour before each contest, with one message for
contests that start together, such as a Div. 1 and a Div. 2 round, and with
start posts on, it posts again as they start. If a contest they were reminded of moves, is
cancelled or comes back, it tells them. A Codeforces, AtCoder, CodeChef,
LeetCode or TopCoder contest that drops off its site's list before it starts
counts as cancelled once three reads in a row have missed it, the last at least
20 minutes after it was last listed. One that has started never does: AtCoder
drops contests from its list as they start. ICPC contests, the regionals and
the World Finals, are never cancelled this way, because the bot reads only some
ICPC contests; the bot owner can still move one with `/kcpc contests settime`.

CodeChef, LeetCode, TopCoder and the ICPC World Finals come from clist.by,
which lists the contests of many sites, once `CLIST_USERNAME` and
`CLIST_API_KEY` are set (see §5). To get a key, sign up for a free account at
[clist.by](https://clist.by), then open its API documentation page,
<https://clist.by/api/v4/doc/>, which shows your username and API key. The
World Finals count as `icpc` contests. Without the key, the `codechef`,
`leetcode` and `topcoder` platforms have no contests; admins can narrow a
server's platforms with `/kcpc contests platforms`. CodeChef's events that
aren't contests, such as its Placement Prep Weekends, are left out.

ICPC contests come from icpc.global: those in `ICPC_CONTEST_CODES` (see §5).
icpc.global gives only the dates of their events, so each is listed by its
event's first day, as "time TBA", and gets no reminders until the bot owner
sets the contest's own time with `/kcpc contests settime`. Until then, a
contest whose event lasts several days, such as NWERC (27-29 November 2026),
drops out of `/contests upcoming` from that first day. The bot owner can also
add the club's own contests:

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
follow unless they pick platforms without it). That is why
`/kcpc contests add`, `settime`, `remove` and `sync` are the bot owner's
commands (see §3), which work in any channel; they stay in the slash list of
`/kcpc`, but refuse everyone else. Keep the bot in the club's own servers
only (`ALLOWED_GUILD_IDS`, and in the Discord Developer Portal, under Bot,
turn off Public Bot), and try these commands with a separate test bot.

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
only one member of a server. Each `/link` command can be used once every 10
seconds by each member, and the Verify button follows `/link verify`'s rule:
who may use it, and whether this server switched it off, but not channels.
Verifying, with the button or `/link verify`, also works once every 10
seconds for each member, as each try asks the site for the profile.

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
`/unlink codeforces` can't remove it: moderators and admins (`TLE_MODERATOR`,
`TLE_ADMIN`) change it with `;handle set` and remove it with
`;handle remove`, which have no slash form. TLE keeps the handle of a member
who leaves, for if they come back, so nobody else can link it until it is
removed. Where Codeforces OAuth is set up, TLE's `/handle identify` links a
Codeforces account too, telling the member privately how it went. AtCoder
links are KCPC's own, and TLE's features don't use them.

#### Weekly problem and /randproblem

To have the bot post a weekly problem in a server:

```text
/kcpc channel weekly #weekly-problem   where the problems and solutions go
/kcpc role weekly role:@Weekly         a pings-only role each problem mentions
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
`/kcpc weekly rotation entries:default` goes back to the default rotation, and
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
/kcpc role algo role:@Algo       a pings-only role each topic mentions
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
The bot will appear online in your Discord server. Give the server a staff
channel and a bot channel with `/access` (step 4 of §4), then use `/help`
inside Discord to explore commands.

Compose restarts the bot after a crash or a reboot (`restart: unless-stopped`),
so the bot owner's `;meta kill` restarts it; stop it with
`docker compose stop`.

### Updating to a new release

```sh
git pull
docker compose build --pull    # fetch newer base images
docker compose up -d           # recreate the container on the new image
```

Coming from a version without the access rules of §3, follow the steps of
§4 too.

---

## 3 · Access: where commands work, and who may use them

Every command has a rule: who may use it, and where it answers. Each
server's admins choose its bot channels and its staff channel with
`/access`, and can tighten any command's rule there.

### Bot channels and the staff channel

- **Bot channels** are where member commands answer publicly. Add them with
  `/access bot-channels add`. Elsewhere, member slash commands still work but
  answer only the member who used them, and member `;` commands are refused
  with a short note, deleted after 20 seconds.
- Commands that involve or ping other members work in bot channels alone,
  slash or not: `;duel challenge`, `/duel accept`, `decline`, `withdraw`,
  `draw`, `complete`, `invalidate` and `ranklist`, `;ratedvc`, `;ranklist`,
  `/vcratings`, `/gudgitters`, `;handle list`, `/handle refer` and `/rank`.
- **The staff channel** is where most staff commands work. Set it with
  `/access staff-channel`, and let only staff read it. It counts as a bot
  channel too. Outside it, staff slash commands answer only you and staff `;`
  commands are refused. A few moderator commands, such as `;handle set` and
  `/_nogud`, work in any bot channel, and a few, such as `/kcpc status`, work
  in the staff channel alone.
- A few setup commands act on the channel they are used in, such as
  `/starboard here` and `/set_ratedvc_channel`, so they work in any channel.
  They refuse threads: a thread closes after a while, and the bot then can't
  find it.
- A thread counts as the channel it is in.
- Until a server has a bot channel, no channel is one: slash commands answer
  privately everywhere, and member `;` commands are refused. Until it has a
  staff channel, or once its staff channel is deleted, staff `;` commands are
  refused too, except `;access` and its subcommands, which then work in any
  channel, so that an admin can set one.

### Who is who

| Level | Who passes |
| --- | --- |
| Everyone | every member of the server |
| Trusted | members with the role `TLE_TRUSTED` names, and moderators |
| Moderator | members with the role `TLE_MODERATOR` names, and admins |
| Admin | members with the role `TLE_ADMIN` names, or with the Manage Server permission |
| Developer | members with the role `TLE_DEVELOPER` names (by ID), and admins |
| Bot owner | the owner of the bot's application in the Discord Developer Portal, or the admins and developers of the team that owns it |

- Staff are moderators, developers, admins and the bot owner.
- Being the bot owner doesn't make you an admin, nor does being an admin make
  you the bot owner. The owner's commands concern every server the bot is in:
  `;meta kill`, `;meta guilds`, `;cache …` and the club contests
  (`/kcpc contests add`, `settime`, `remove` and `sync`). They work in any
  channel, for the owner alone; the club contest commands also need the owner
  to be an admin of the server they are used in (Manage Server or the
  `TLE_ADMIN` role).
- The bot finds its owners when it starts, so changes to the application's
  team take effect after a restart.
- `/help` with a command says who may use it, and where.

### What members see

- `/help` answers only the member who asked, and lists the commands they can
  use where they ask.
- A member slash command outside the bot channels answers privately: Discord
  marks the answer "Only you can see this".
- `;` commands work in bot channels only, and most staff ones in the staff
  channel alone. A refused `;` command with a slash form says so: "Or use
  `/x` here: only you will see the answer."
- Members don't see staff commands in their slash lists. The trees of
  moderators' commands (`/_nogud`, `/_unregistervc`, `/roleupdate`) need
  Manage Messages to be seen, and the other staff trees (`/access`, `/kcpc`,
  `/starboard`, `/set_ratedvc_channel`, `/_updatestatus`) need Manage Server.
  Staff commands in members' groups, such as `;handle set`, have no slash
  form, and neither has `;cache`.
- A command that a member may not use gets no answer at all on `;`, as if it
  didn't exist, and a private "You can't use this command." on slash.
  Refusals never name a role or an ID, and never show members the staff
  channel.
- On `;`, refusals are deleted after 20 seconds, and cooldown notes once the
  command works again.
- Only the member who asked can turn the pages of a paged answer.
- A private answer that comes more than 15 minutes after its slash command is
  dropped, never posted for everyone to see.

### /access, for admins

```text
/access show                                  the settings, and what is wrong with them
/access bot-channels add #bot-commands        make a channel a bot channel (25 at most)
/access bot-channels remove #general          make it an ordinary channel again
/access staff-channel channel:#staff          set the staff channel; with no channel, clear it
/access limit command:duel register off:yes   limit a command in this server
/access reset command:duel register           clear a command's limits; all clears every limit
```

- Discord shows `/access` only to members with Manage Server, and its answers
  are private. Admins by role alone use `;access …` in the staff channel, or
  see the Integrations note below.
- Until there is a staff channel, or once it is deleted, `;access`'s
  subcommands work in any channel. `;access` itself then only points to
  `/access show` and `/access staff-channel`, as its answer would be public.
- `/access show` lists the bot channels, the staff channel, the developer role
  and every limit. It warns about: no bot channel or staff channel; a staff
  channel that @everyone can read; channels that no longer exist;
  `TLE_ADMIN`, `TLE_MODERATOR`, `TLE_TRUSTED` or `TLE_DEVELOPER` matching no
  role here, several, or @everyone (by the server's ID or by name), which
  never counts; `;ratedvc`'s channel outside the bot channels; and
  settings that aren't stored (under `--nodb`). It notes when
  `ALLOWED_GUILD_IDS` isn't set.

#### Limits

A limit tightens a command's rule in one server; it never loosens it.

```text
/access limit command:duel register off:yes   switch ;duel register off
/access limit command:gitgud where:bot-only   /gitgud in bot channels alone, slash too
/access limit command:plot who:trusted        the plot commands for trusted members
/access limit command:rank private:yes        /rank answers only the member who uses it
/access reset command:all                     clear every limit
```

- `who` (`trusted`, `moderator`, `developer` or `admin`): only those members,
  and admins, may use it.
- `where` (`bot`, `bot-only`, `staff` or `staff-only`): where it answers
  publicly. Elsewhere, its slash command answers privately, and the `-only`
  places refuse it there too.
- `private: yes`: its slash answers are always private, and its `;` form
  stops working. A command without a slash form refuses this.
- `off: yes`: the command is switched off in this server.
- `subcommands`: whether a limit on a group covers its subcommands. It does
  when you name the group, so `command:duel off:yes` switches off every duel
  command. A slash fallback, such as `clist show`, or a twin, such as
  `contests upcoming`, limits the group's own command alone, unless you add
  `subcommands:yes`.
- Each `/access limit` merges into the command's limit: the options you leave
  out stay as they are. Where several limits apply, members must pass every
  `who`, the strictest `where` wins, and `private` and `off` apply if any
  limit sets them.
- `/help`, `/access` and the bot owner's commands take no limits.
- Limits don't change members' slash lists, which are the same in every
  server. To hide a command there too, see the Integrations note below.

### /help

```text
/help                       the commands you can use here
/help command:clist future  how to use one command
;help gitgud                the same, for everyone in the channel to see
```

- `/help` lists the commands you can use where you ask, a page for each
  category, with the form that works there: `/x`, or `;x` alone. Only you see
  the answer, and only you can turn its pages.
- `/help` with a command shows what it does, how to type it, its options and
  examples, who may use it and where, what it does in this channel, and its
  cooldown. Admins also see its default rule and this server's limits on it.
- A group's help lists its subcommands, and marks those that work only
  elsewhere, such as "(bot channels only)".
- `;help` answers the whole channel, so it lists only the commands for
  everyone. For any other command it says "Use `/help x` for this command."
- As you type, `/help` suggests the commands you can use where you are.

### Integrations: staff slash commands for moderators

Discord shows a hidden slash command only to members with the permission it
needs: Manage Messages for the moderators' trees, Manage Server for the rest.
A moderator, developer or admin who has only the role doesn't see them, but
can use the `;` forms in the staff channel. To show them the slash commands
too, open
Server Settings → Integrations → the bot, choose the command, and add the
role. This changes only who sees the command: the access rules still decide
who may use it.

### Manage Server counts as admin

Members with the Manage Server permission are admins, whatever their roles,
for TLE's commands as well as KCPC's: they can use `/access`, `/starboard`,
`;handle grandfather` and every other admin command. Give Manage Server to
admins alone.

### When something is wrong

The rules fail closed:

- A command missing from the rule table is for the bot owner alone, in the
  staff channel, and the bot logs a warning when it starts.
- If a server's stored access settings can't be read, every command but
  `/access`, `/help` and the bot owner's is refused there until an admin uses
  `/access reset all`, which clears every limit and repairs them; then set the
  bot channels and the staff channel again, as their settings were lost too.
  The log names the server. A stored limit that can't be read switches its
  command off.
- If the bot can't read the access settings at all when it starts, such as
  when a row of the `access_settings` table in `user.db` has a guild ID that
  isn't one, every server refuses every command but `/access`, `/help` and
  the bot owner's, and no server's settings can be changed, so that nothing
  is written over settings that were never read. The log says why: the bot
  owner fixes or deletes the bad row in `user.db`, then restarts the bot.
- Under `--nodb`, the settings are kept in memory and lost when the bot stops;
  `/access` says so.

---

## 4 · Upgrading

After deploying the version with these access rules, do this once:

1. In the Discord Developer Portal, open the bot's application. Under Bot,
   turn off **Public Bot**, so that only you can add the bot to a server. If
   others look after the bot with you, add them to the application's team, as
   admins or developers: the bot owner's commands are for its owner and those
   team members alone. They can use the club contest commands only in servers
   where they are admins.
2. In `.env`, set `TLE_ADMIN`, `TLE_MODERATOR` and `TLE_TRUSTED` to role IDs,
   and `TLE_DEVELOPER` to the ID of a developer role, if you want one (see
   §5).
3. Optionally, set `ALLOWED_GUILD_IDS` to the club's servers (see §5).
4. Restart the bot (`docker compose up -d --build`). Then, in each server:

   ```text
   /access staff-channel channel:#staff
   /access bot-channels add #bot-commands
   /access show
   ```

   An admin who has the admin role but not Manage Server doesn't see
   `/access`: they can type `;access staff-channel #staff` in any channel,
   then `;access bot-channels add #bot-commands` in the staff channel.

Until then, no channel is a bot channel: slash commands answer only the member
who used them, and member `;` commands are refused with a note saying so.

What else changes:

- Members with Manage Server are admins for TLE's commands too.
- `;meta kill`, `;meta guilds`, `;cache` and the club contest commands are the
  bot owner's alone, no longer every admin's.
- Staff commands leave members' slash lists. Discord can take a minute to
  update them; until then, a command it still shows answers "This command has
  changed. Try again in a minute."
- The bot pings neither @everyone nor roles unless a message allows it, as
  contest reminders do for their role.
- Slash commands work in servers alone, and the bot installs in servers alone.

---

## 5 · Environment variables ( `.env` )

| Variable | Required | Example | Description |
|----------|----------|---------|-------------|
| `BOT_TOKEN` | ✅ | `MTEz…` | Discord bot token from the Dev Portal |
| `LOGGING_COG_CHANNEL_ID` | ✅ | `123456789012345678` | channel where uncaught errors are sent |
| `ALLOW_DUEL_SELF_REGISTER` | ❌ | `true` | let users self-register for duels |
| `ALLOWED_GUILD_IDS` | ❌ | `123456789012345678,234567890123456789` | the servers the bot may be used in, by ID; unset, any server (see below) |
| `TLE_ADMIN` | ❌ | `123456789012345678` | the admin role, by ID (recommended, see below) or name; unset, the role named `Admin` |
| `TLE_MODERATOR` | ❌ | `234567890123456789` | the moderator role, by ID (recommended) or name; unset, the role named `Moderator` |
| `TLE_TRUSTED` | ❌ | `345678901234567890` | the trusted role, by ID (recommended) or name; unset, the role named `Trusted` |
| `TLE_PURGATORY` | ❌ | `Purgatory` | the purgatory role, by ID or name; unset, the role named `Purgatory` |
| `TLE_DEVELOPER` | ❌ | `456789012345678901` | the developer role, by ID only; unset, there is none (see below) |
| `DISABLED_EXTENSIONS` | ❌ | `tle.duel,tle.graphs` | extensions, or families (`tle`, `kcpc`), to switch off |
| `KCPC_TIMEZONE` | ❌ | `Europe/London` | the club's time zone, for schedules and times admins type |
| `KCPC_DB_PATH` | ❌ | `data/db/kcpc.db` | where the KCPC database lives |
| `HTTP_USER_AGENT` | ❌ | `KCPC-bot (+https://…)` | User-Agent of KCPC's requests to other sites |
| `LUMA_CALENDAR_ID` | ❌ | `cal-…` | default Luma calendar (its ID), for servers that haven't set one |
| `ICPC_CONTEST_CODES` | ❌ | `UKIEPC,Northwestern-Europe-2027` | icpc.global contests to track, by abbreviation; icpc.global gives their dates, the bot owner sets their times (`/kcpc contests settime`) |
| `CLIST_USERNAME`, `CLIST_API_KEY` | ❌ | | clist.by username and API key; with both set, the bot also tracks CodeChef, LeetCode, TopCoder and the ICPC World Finals (see §1) |

Give `TLE_ADMIN`, `TLE_MODERATOR` and `TLE_TRUSTED` as role IDs: a name
matches a role of that name in every server the bot is in, whoever made it.
To copy a role's ID, turn on Developer Mode in Discord (User Settings →
Advanced), then right-click the role under Server Settings → Roles.
`TLE_DEVELOPER` takes an ID only; a name is ignored, with a warning when the
bot starts. Members with the developer role can use the developer commands,
`;kcpc status` and `;meta git`, in the staff channel, which admins can use
too; with no developer role, only admins can. §3 says what each role may do;
`/access show` warns when a role setting matches no role in the server,
several, or @everyone, which never counts: don't give the server's ID as a
role's.

`ALLOWED_GUILD_IDS` keeps the bot to the servers it lists, by ID (with
Developer Mode on, right-click a server's icon to copy its ID). The bot
doesn't start if an item isn't an ID. In any other server, its commands are
refused: `;` commands get no reply, and slash commands a short private notice.
If the bot is added to a server that isn't listed, it leaves at once, with a
warning in the log. When it starts, it only logs the servers it is already in
that aren't listed, and stays in them, so a mistyped ID can't make it leave
yours.
In those servers, what was set up before, such as reminders, keeps working,
but no one there can change it: remove the bot from them yourself. Also turn
off Public Bot in the Discord Developer Portal (under Bot), so that only you
can add the bot to a server.

Feel free to add any extra variables your cogs consume; Compose passes
every key in `.env` to the container. Run without Docker, the bot reads
`.env` itself; variables already set in the environment take precedence.

---

## 6 · Data folder

`docker compose` mounts `./data` into the container. It holds:

- `db/user.db`: TLE's server data, such as members' Codeforces handles (those
  linked with `/link codeforces` too), duels, reminder settings, starboards
  and each server's access settings (its bot channels, staff channel and
  limits).
- `db/kcpc.db`: each server's KCPC settings, the workshops and contests the
  bot has read (and the contests and times the bot owner has set), members'
  AtCoder links and the links waiting to be verified, the ratings last read for
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
- `db/cache.db`: TLE's Codeforces cache. The bot refills most of it by itself,
  but the bot owner has to refill the rating changes and problemsets with
  `;cache ratingchanges all` and `;cache problemsets all`.
- `misc/contest_writers.json`: an optional list of contest writers, made with
  `extra/scrape_cf_contest_writers.py`.
- `temp/`: images the bot is drawing.

Only `db/cache.db` (then refill it as above) and `temp/` are safe to delete.
Keep the rest and back it up: losing `kcpc.db` loses every server's KCPC
setup, members' AtCoder links and the record of what was posted, so reminders
could go out again.

To back up the databases, stop the bot (`docker compose stop`) and copy
`data/db`, or use sqlite3's `.backup` command while it runs. The databases use
WAL mode, so while the bot runs, recent writes can still be in the `-wal` file
next to each one, and a copy of the database file alone can miss them.

---

## 7 · Local development (optional)

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

## 8 · Repository layout

```sh
.
├─ Dockerfile              # 2-stage image, installs native cairo stack
├─ compose.yaml            # single-service compose file
├─ requirements.txt        # runtime Python deps (no pins)
├─ .env.example            # template for your secrets
├─ data/                   # databases & caches, see §6 (git-ignored)
├─ tle/ …                  # bot source code
└─ extra/ fonts.conf …     # helper resources
```

---

## 9 · Contributing

Pull requests are welcome!  
Before opening a PR, please

1. run `ruff check --fix .` (auto-formats touched lines),
2. keep commits focused; large refactors in a separate PR.

---

## 10 · License

MIT ― see `LICENSE`.
