"""No password in an address ever reaches a log line written in this container.

CarConnectivity's database plugin logs its config at info, connection URL and password
included, and any library can put a URL in an exception. Every log record made in any
Python process here (sitecustomize.py installs this) passes through ScrubFilter as it is
created: scheme://user:secret@host becomes scheme://user:***@host in the message, its
arguments, the traceback and the stack. Installed on the record factory rather than on a
handler, because CarConnectivity adds its handlers (logging.basicConfig) only after this
has run, and a filter on a logger does not see records its children propagate.
"""

from __future__ import annotations

import logging
import re

URL_CREDENTIALS = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@'\"]*:)[^\s@'\"]+@")


def scrub(text: str) -> str:
    return URL_CREDENTIALS.sub(r"\1***@", text)


class ScrubFilter(logging.Filter):
    """Mask URL passwords in a record; never drops one."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed record is the logging module's to report
            return True
        clean = scrub(msg)
        if clean != msg:
            record.msg, record.args = clean, ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub(record.stack_info)
        return True


def install() -> None:
    """Scrub every record this process creates, whichever handler later writes it. Never raises."""
    try:
        old = logging.getLogRecordFactory()
        if getattr(old, "_cc_scrubbed", False):
            return
        f = ScrubFilter()

        def factory(*args, **kwargs):
            record = old(*args, **kwargs)
            f.filter(record)
            return record

        factory._cc_scrubbed = True
        logging.setLogRecordFactory(factory)
        for h in logging.getLogger().handlers:  # and any handler already on the root
            h.addFilter(f)
    except Exception as err:  # noqa: BLE001 - logging must keep working whatever happens here
        logging.getLogger("carcommand.logscrub").warning("could not install the log scrub: %s", type(err).__name__)
