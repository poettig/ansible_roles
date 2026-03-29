#!/var/lib/matrix-notifier/venv/bin/python3

import argparse
import fileinput
import logging

import requests


def send_notification(name: str, message: str, recipients: list | str) -> tuple[int, str]:
    if isinstance(recipients, str):
        recipients = [recipients]

    message_with_linebreaks = message.replace("\\n", "\n")

    json_data = {"message": f"**{name}** wrote:\n\n{message_with_linebreaks}", "rooms": recipients}
    resp = requests.post("http://localhost:3578/send-markdown", json=json_data, timeout=60)
    return resp.status_code, resp.text


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send a notification via the oettig.de Telegram InfrastructureNotifier Bot."
    )
    parser.add_argument("name", help="Name of the Bot shown in the chat.")
    parser.add_argument("message", help="Notification message to send")
    parser.add_argument(
        "recipients",
        metavar="recipients",
        nargs="*",
        default="peet",
        help="Name(s) of the recipient(s) of the notifications. Default recipient is 'peet'.",
    )
    args = parser.parse_args()

    # Read message from stdin if "-"
    message = "".join(list(fileinput.input("-"))) if args.message == "-" else args.message

    status_code, body = send_notification(args.name, message, args.recipients)
    if status_code >= 300:
        logging.error(f"Error sending notification: {status_code} - {body}")


if __name__ == "__main__":
    main()
