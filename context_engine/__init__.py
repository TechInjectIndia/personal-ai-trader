"""Context Engine — standalone FastAPI microservice.

Owns news/context ingestion, LLM conviction scoring, persistence, and a
read API. NEVER imported by the bot's cron loop; the only contract is HTTP
(localhost-only, 127.0.0.1:8601). It MAY import ``helm.llm`` and
``helm.data.store`` — that direction (service -> helm) is allowed and shares
``dbname=helm`` over the local socket. The prohibition is the reverse:
nothing under ``helm/`` or ``scripts/`` (the bot loop) imports this package.
"""
