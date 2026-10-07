"""Who may use each command, in which channels, and who sees the answers.

Every command has a default rule: the members it is for, and the channels where
it answers publicly. Elsewhere, a slash command answers only the member who
used it and a prefix command is refused. A server's admins choose its bot
channels and its staff channel, and can tighten any command's rule with a
limit, but never loosen it.

The pure modules import nothing from Discord:

- ``rules``: the model (who, where, limits, the member asking and the channel)
  and ``decide``, which gives the outcome of one use of a command;
- ``settings``: a server's channels and limits, and the JSON they are stored
  as, read so that a bad row fails closed;
- ``table``: every command's default rule, the twins that share one, the
  limit keys that apply to a command, and the categories of /help;
- ``policy``: a command's rule in one server, the table's rule with the
  server's limits, and rules in plain English.

The rest connects them to Discord:

- ``service``: ``AccessService``, which holds every server's settings in memory,
  checks every command before it runs and answers refusals, and the command
  tree that checks slash commands and their autocomplete;
- ``context``: the command context that makes answers private when the
  decision says so;
- ``slash``: hides staff commands from members' slash lists;
- ``help`` and ``cog``: /help, and /access for admins.

KCPC never imports this package: it reaches the service only as the bot's
``access`` attribute.
"""
