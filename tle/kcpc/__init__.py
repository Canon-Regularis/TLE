"""KCPC club features for the TLE bot.

The package is layered, lowest first. Each layer imports only from the layers
below it, which tests/kcpc/unit/test_architecture.py checks:

- ``core``: infrastructure with no Discord imports: time and schedules, the job
  scheduler, the database and its migrations, the delivery ledger that keeps
  every automatic post at most once, per-server settings and the HTTP client.
- ``bot``: the Discord toolkit shared by KCPC cogs: the base cog, checks,
  embeds, views and the publisher that sends posts and recovers after crashes.
- ``platforms``: adapters for external sites such as Luma and AtCoder.
- ``features``: one package per feature, each with a thin cog on top; features
  never import each other.

``services`` holds what the features run on, and ``bootstrap`` builds it when
the bot starts.
"""
