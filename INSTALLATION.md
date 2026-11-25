# Installation

From a clone to a working bot in your Discord server. [README.md](README.md)
describes the features and every setting.

You need:

- [Git](https://git-scm.com/downloads), and
  [Docker](https://docs.docker.com/get-started/get-docker/) with Compose
- a Discord server where you have the Manage Server and Manage Roles permissions

## 1. Create the Discord application

In the [Discord Developer Portal](https://discord.com/developers/applications),
select **New Application**. The account or team that owns the application is the
**bot owner**, the only one who can use the bot-wide commands of step 6.

| Page | Setting |
| --- | --- |
| **Installation** | Set **Install Link** to **None**, or Discord won't let you turn off **Public Bot** |
| **Bot** | Turn off **Public Bot**, so that only you can invite the bot |
| **Bot** | Turn on **Server Members Intent** and **Message Content Intent**. Without both, the bot can't connect |
| **Bot** | Select **Reset Token**, and copy the token for step 3 |
| **General Information** | Copy the **Application ID** for step 2 |

Keep the token secret: whoever has it controls the bot.

## 2. Prepare the server

1. Create three text channels: `#staff` and `#bot-log`, which only staff can
   read, and `#bot-commands`. Create one more for each club feature you want
   (step 5), such as `#contests`.
2. Open this link with your application ID in place of `APPLICATION_ID`, and add
   the bot to your server:

   ```text
   https://discord.com/oauth2/authorize?client_id=APPLICATION_ID&scope=bot+applications.commands&permissions=275146460160
   ```

   It asks for View Channels, Send Messages, Send Messages in Threads, Embed
   Links, Attach Files, Read Message History and Manage Roles.
3. Let the bot's role see `#staff` and `#bot-log`, and post in them.
4. Create a role for each Codeforces rank, named exactly as here, with no
   permissions. Members can't link a rated account whose rank has no role.

   ```text
   Newbie
   Pupil
   Specialist
   Expert
   Candidate Master
   Master
   International Master
   Grandmaster
   International Grandmaster
   Legendary Grandmaster
   ```

5. Under Server Settings → Roles, move the bot's role above the rank roles, and
   above any other role that it should give.

The bot treats roles named `Admin`, `Moderator`, `Trusted` and `Purgatory` as its
own. If your server uses those names for something else, set the `TLE_…` roles in
step 3 by ID.

## 3. Configure

```sh
git clone https://github.com/Canon-Regularis/TLE.git
cd TLE
cp .env.example .env
```

Edit two lines of `.env`:

| Variable | Value |
| --- | --- |
| `BOT_TOKEN` | the token from step 1 |
| `LOGGING_COG_CHANNEL_ID` | the ID of `#bot-log` |

To copy an ID, turn on User Settings → Advanced → Developer Mode in Discord, then
right-click the channel, role or server.

The other variables are optional. Consider `ALLOWED_GUILD_IDS`, which limits the
bot to your servers, and `KCPC_TIMEZONE`, which the weekly and monthly posts
follow.

## 4. Start

On Linux, first create the data folder for the container's user (ID 1000):

```sh
mkdir -p data
sudo chown 1000:1000 data
```

Then, on any system:

```sh
docker compose up -d --build
docker compose logs -f
```

The bot is up when the log says `Bot running`, which it also posts in `#bot-log`.
On the first start, two warnings about empty caches come before it; step 6 fills
those caches.

## 5. Set up the server in Discord

Choose where commands work. Until you do, slash commands answer only the member
who used them, and `;` commands are refused.

```text
/access staff-channel channel:#staff
/access bot-channels add channel:#bot-commands
/access show
```

`/access show` lists what is still wrong with the setup.

Then turn on the club features you want, each with the channel it posts in:

| Feature | Posts |
| --- | --- |
| `contests` | contest reminders, and members' results |
| `weekly` | a problem each Friday |
| `algo` | an algorithm on the 1st of each month |
| `workshops` | reminders of the workshops in a Luma calendar |

```text
/kcpc channel feature:contests channel:#contests
/kcpc enable feature:contests
```

`workshops` also needs its calendar:

```text
/kcpc workshops calendar calendar:<calendar ID or iCal link>
```

`/kcpc show` lists the settings, and any problem with their channels and roles.

## 6. Fill the caches

As the bot owner, send these in `#staff`, each once the one before has replied
`Completed`:

```text
;cache contests
;cache problems
;cache problemsets all
;cache ratingchanges
```

The last one takes the longest, as it asks Codeforces for one contest at a time.
Members can use the bot meanwhile, but plots and recommendations that use past
contests are incomplete until it ends.

## 7. Check that it works

| Command | Expected result |
| --- | --- |
| `/meta ping` | the bot's latency |
| `/help` | the commands you can use |
| `/link codeforces` with your handle | a token and the steps to verify your account, then your rank role |
| `/kcpc status` in `#staff` | the state of the club features |

## Troubleshooting

`docker compose logs` shows why a start failed. After changing `.env`, run
`docker compose up -d` again: `docker compose restart` keeps the old values.

| Symptom | Fix |
| --- | --- |
| `Token required`, or `Improper token has been passed` | Set `BOT_TOKEN` to the token from step 1 |
| `PrivilegedIntentsRequired` | Turn on both intents (step 1) |
| `invalid literal for int()` from `tle.cogs.logging` | Set `LOGGING_COG_CHANNEL_ID` to a channel ID |
| `Permission denied: 'data/db'` | Give the data folder to the container's user (step 4) |
| Port 8080 is already in use | Set `OAUTH_SERVER_PORT` in `.env` to a free port |
| `The log channel … is unreachable` | Let the bot's role see `#bot-log` and post in it |
| `;` commands get no answer | You may not use that command, or Message Content Intent is off. `/help` lists yours |
| `/access` and `/kcpc` don't appear | You need Manage Server |
| `Role for rank … not present in the server` | Create that rank's role (step 2) |
| The bot can't change roles | Move its role higher (step 2) |

To update the bot, see [Updating to a new release](README.md#updating-to-a-new-release).
