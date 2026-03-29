#!/var/lib/mail2notify/venv/bin/python3

import html
import inspect
import logging
import mailbox
import os
import pathlib
import re
import socket
import sys
import textwrap
import time
import typing

import requests

MAIL_PATH = "/var/spool/mail"


def mail_generator(path: pathlib.Path) -> typing.Generator[mailbox.mboxMessage, None, None]:
    uid = path.stat().st_uid
    gid = path.stat().st_gid

    mbox = mailbox.mbox(path, create=False)

    # Try to acquire lock until successful
    while True:
        try:
            mbox.lock()
            break
        except mailbox.ExternalClashError:
            logging.info(f"Failed to aquire lock on {path}, trying again...")
            time.sleep(1)

    # Ensure that the lock is released even if some weird error is thrown, like KeyboardInterrupt or smth.
    try:
        # Make a copy to iterate on because the mbox will be modified while iterating it otherwise
        mails = list(mbox.iteritems())
        for key, message in mails:
            mbox.remove(key)
            yield message
    finally:
        mbox.unlock()
        mbox.close()

        # Fix permissions of the mailbox file
        # The mailbox module can't change them as non-root cannot change ownership of a file
        # This means this script has to run as root
        os.chown(path, uid, gid)


def message_to_chunks(message: str) -> list[str]:
    chunks = []
    lines = message.split("\n")
    char_limit = 10000

    chunk = ""
    for line in lines:
        if len(line) > char_limit:
            # Wrap into 3500 character parts
            for part in textwrap.wrap(line, char_limit):
                if len(part) == char_limit:
                    chunks.append(part)
                    chunk = ""
                else:
                    chunk = part

            # Re-add the linebreak that was split away
            chunk += "\n"
        else:
            if len(chunk) + len(line) > char_limit:
                chunks.append(chunk)
                chunk = ""

            chunk += line + "\n"

    if chunk:
        chunks.append(chunk)

    return chunks


def process_mailbox(file: str, mbox_path: pathlib.Path) -> None:
    logging.info(f"Processing non-empty file {mbox_path}...")
    for mail in mail_generator(mbox_path):
        header = inspect.cleandoc(
            f"""
            User `{file}` received local mail on `{socket.getfqdn()}`!

            From: {mail["from"]}
            To: {mail["to"]}
            Date: {mail["date"]}
            Subject: {mail["subject"]}
            """
        )

        if mail.is_multipart():
            message = "\n----- Multipart Separator -----\n\n".join([str(part) for part in mail.get_payload()])
        else:
            message = mail.get_payload()

        message_chunks = message_to_chunks(message)

        for idx, part in enumerate(message_chunks):
            message = "" if idx != 0 else html.escape(header) + "\n\n"

            # Prevent escaping Markdown code block by replacing backtick with "Modifier Letter Grave Accent" homoglyph
            escaped_part = re.sub(r"```\n", "\u02cb\u02cb\u02cb\n", part)
            linebreak = "\n" if part[-1] != "\n" else ""
            message += f"```text\n{escaped_part}{linebreak}```"
            if idx + 1 != len(message_chunks):
                message += "\nMessage too long, continuing in followup message."

            response = None
            try:
                response = requests.post(
                    "http://localhost:3578/send-markdown", json={"message": message, "rooms": ["peet"]}, timeout=10
                )
            except requests.RequestException as e:
                logging.error(f"Failed to send notification: {e}")

            if response and response.status_code != 200:
                logging.error(f"Failed to send notification: {response.status_code} - {response.content}")

        response = None
        try:
            # Send mail as file
            response = requests.post(
                "http://localhost:3578/send-file",
                files={"file": ["mail.eml", mail.as_bytes(), "message/rfc822"]},
                data={"rooms": ["peet"]},
                timeout=10,
            )
        except requests.RequestException as e:
            logging.error(f"Failed to send notification: {e}")

        if response and response.status_code != 200:
            logging.error(f"Failed to send notification: {response.status_code} - {response.content}")


def main() -> None:
    if os.getuid() != 0:
        logging.error("This script has to fiddle with files of other users. Please run as root.")
        sys.exit(1)

    while True:
        try:
            for path, _, files in os.walk(MAIL_PATH):
                for filename in files:
                    mbox_path = pathlib.Path(path) / pathlib.Path(filename)
                    if mbox_path.stat().st_size > 0:
                        process_mailbox(filename, mbox_path)

        except (FileNotFoundError, mailbox.NoSuchMailboxError) as e:
            logging.warning(e)

        time.sleep(1)


if __name__ == "__main__":
    main()
