"""Notification channels: console and e-mail (SMTP). They only report signals, never place bets."""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

from rich.console import Console

from polledge.config import EmailSettings, NotifySettings
from polledge.errors import ConfigError
from polledge.notify.base import Notifier, Signal
from polledge.i18n import _, decimal

log = logging.getLogger(__name__)


def format_signal(s: Signal) -> str:
    stake = f"{s.stake:,.0f}".replace(",", " ")
    return _(
        "{market}: {outcome} - odds {odds} ({bookmaker}), model {p_model}, edge {edge}, stake {stake} {currency}",
        market=s.market_label, outcome=s.outcome_label, odds=decimal(f"{s.odds:.2f}"), bookmaker=s.bookmaker,
        p_model=decimal(f"{s.p_model:.1%}"), edge=decimal(f"{s.edge:+.1%}"), stake=stake, currency=s.currency,
    )


class ConsoleNotifier(Notifier):
    def __init__(self, console: Console | None = None):
        self.console = console or Console()

    def send(self, election_name: str, signals: list[Signal]) -> None:
        self.console.print("[bold green]" + _("New signals - {election} ({count}):", election=election_name, count=len(signals)) + "[/bold green]")
        for s in signals:
            self.console.print(f"  * {format_signal(s)}")


class EmailNotifier(Notifier):
    def __init__(self, settings: EmailSettings):
        if not (settings.smtp_host and settings.sender and settings.recipients):
            raise ConfigError(_("E-mail notifications need notify.email.smtp_host, sender and recipients in settings.yaml"))
        self.settings = settings

    def send(self, election_name: str, signals: list[Signal]) -> None:
        s = self.settings
        msg = EmailMessage()
        msg["Subject"] = _("PollEdge: {count} new signals - {election}", count=len(signals), election=election_name)
        msg["From"] = s.sender
        msg["To"] = ", ".join(s.recipients)
        body = [_("New value signals ({election}):", election=election_name), ""]
        body += [f"- {format_signal(sig)}" for sig in signals]
        body += ["", _("PollEdge only analyses and recommends; it never places bets.")]
        msg.set_content("\n".join(body))
        password = os.environ.get(s.password_env, "")
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
            if s.use_tls:
                smtp.starttls()
            if s.username:
                smtp.login(s.username, password)
            smtp.send_message(msg)
        log.info("Signal e-mail sent to %s", s.recipients)


def build_notifiers(settings: NotifySettings, console: Console | None = None) -> list[Notifier]:
    notifiers: list[Notifier] = []
    for channel in settings.channels:
        if channel == "console":
            notifiers.append(ConsoleNotifier(console))
        elif channel == "email":
            notifiers.append(EmailNotifier(settings.email))
    return notifiers
