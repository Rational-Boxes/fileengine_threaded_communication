"""SMTP delivery for the digest (SPECIFICATION §11c). Best-effort — a send failure
returns False (the sender leaves the period unmarked for retry) and never raises.
Same SMTP settings shape ldap_manager uses for invite/reset mail.
"""
from __future__ import annotations

import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger("discussion.mailer")


class SmtpMailer:
    def __init__(self, config):
        self.config = config

    @property
    def configured(self) -> bool:
        return bool(self.config.smtp_host and self.config.digest_from)

    def send(self, to_addr: str, subject: str, text: str, html: str) -> bool:
        if not self.configured or not to_addr:
            return False
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.config.digest_from
        msg["To"] = to_addr
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        try:
            import smtplib
            with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=15) as s:
                try:
                    s.starttls()
                except Exception:
                    pass  # server without STARTTLS
                if self.config.smtp_user:
                    s.login(self.config.smtp_user, self.config.smtp_password)
                s.sendmail(self.config.digest_from, [to_addr], msg.as_string())
            return True
        except Exception:
            log.warning("digest send to %s failed", to_addr, exc_info=True)
            return False
