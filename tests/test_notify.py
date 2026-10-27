import pytest
from rich.console import Console

from polledge.config import EmailSettings, NotifySettings
from polledge.errors import ConfigError
from polledge.notify import channels
from polledge.notify.base import Signal
from polledge.notify.channels import ConsoleNotifier, EmailNotifier, build_notifiers, format_signal

SIGNAL = Signal("winner", "Election winner", "PAVEL", "Petr Pavel", "Betano", 1.5, 0.8, 0.2, 250.0, "CZK")


def test_format_signal():
    text = format_signal(SIGNAL)
    assert "Petr Pavel" in text and "1.50" in text and "Betano" in text and "+20.0%" in text and "250 CZK" in text


def test_console_notifier():
    console = Console(record=True, width=200)
    ConsoleNotifier(console).send("CZ 2028", [SIGNAL])
    assert "Petr Pavel" in console.export_text()


def test_email_requires_config():
    with pytest.raises(ConfigError):
        EmailNotifier(EmailSettings())
    with pytest.raises(ConfigError):
        build_notifiers(NotifySettings(channels=["email"]))


def test_email_sends_message(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["host"] = host

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr(channels.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("POLLEDGE_SMTP_PASSWORD", "secret")
    settings = EmailSettings(smtp_host="smtp.example.org", username="me", sender="a@example.org",
                             recipients=["b@example.org"])
    EmailNotifier(settings).send("CZ 2028", [SIGNAL])
    assert sent["host"] == "smtp.example.org" and sent["tls"] and sent["login"] == ("me", "secret")
    assert "Petr Pavel" in sent["msg"].get_content()
