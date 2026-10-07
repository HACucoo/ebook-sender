"""One SMTP session per scan run, opened only when there is something to send."""
from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path
import smtplib
import ssl

from .store import Settings


class Mailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._server: smtplib.SMTP | None = None

    def _connect(self) -> smtplib.SMTP:
        s = self.settings
        context = ssl.create_default_context()
        if s.smtp_security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(s.smtp_host, int(s.smtp_port), timeout=60, context=context)
        else:
            server = smtplib.SMTP(s.smtp_host, int(s.smtp_port), timeout=60)
            if s.smtp_security == "starttls":
                server.starttls(context=context)
        if s.smtp_user:
            server.login(s.smtp_user, s.smtp_password)
        return server

    def send(self, path: Path, to: str, subject: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.settings.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(f"Automatisch von ebook-sender: {path.name}")
        msg.add_attachment(path.read_bytes(), maintype="application", subtype="epub+zip", filename=path.name)
        if self._server is None:
            self._server = self._connect()
        self._server.send_message(msg)

    def close(self) -> None:
        if self._server is not None:
            try:
                self._server.quit()
            except smtplib.SMTPException:
                pass
            self._server = None

    def test(self) -> None:
        """Log in and out — for the settings page."""
        server = self._connect()
        server.quit()
