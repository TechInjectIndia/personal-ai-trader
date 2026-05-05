"""
Helm command-line interface — `helm <command>`.

Commands:
  configure   — guided credential entry into OS keychain
  verify      — ping Kite + IBKR; print account summary
  status      — print orchestrator state, holdings per sleeve, risk usage
  kill        — engage kill-switch immediately
  re-arm      — explicitly re-enable autonomous trading
  pause-strategy <name>
  deploy <strategy>
  run-backtest <strategy>
  replay-audit [--since <iso-ts>]
"""

import click


@click.group()
def main() -> None:
    """Helm orchestrator CLI."""


@main.command()
def configure() -> None:
    """Walk through credential entry into OS keychain."""
    click.echo("TODO Phase 3 — guided keychain prompts for KITE_API_KEY/SECRET, IBKR creds, SMTP, Telegram.")


@main.command()
def verify() -> None:
    """Ping Kite + IBKR; print account summary."""
    click.echo("TODO Phase 3 — call brokers.zerodha.profile() and brokers.ibkr.account_summary().")


@main.command()
def status() -> None:
    """Print orchestrator state."""
    click.echo("TODO Phase 3.")


@main.command()
def kill() -> None:
    """Engage kill-switch."""
    from helm.orchestrator.kill_switch import kill as do_kill
    do_kill(reason="cli")


if __name__ == "__main__":
    main()
