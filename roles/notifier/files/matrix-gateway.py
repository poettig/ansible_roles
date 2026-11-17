#!/usr/bin/env python3

import argparse
import asyncio
import datetime
import enum
import html
import io
import json
import logging
import os.path
import pathlib
import re
import signal
import typing
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import fastapi
import inflect
import markdown
import nio
import pydantic
import pydantic.alias_generators
import uvicorn
from linkify_it import LinkifyIt
from PIL import Image

HIDDEN_LABELS = {"grafana_folder", "repeat_alert"}
MAX_FORMATTED_LENGTH = 15000

matrix_client: nio.AsyncClient | None = None
verification_data: dict | None = None

room_mappings = {
    "peet": "!yQKueemwhhtwODXBHs:matrix.org",
}

inflector = inflect.engine()
linkifier = LinkifyIt().set({"fuzzy_link": False, "fuzzy_email": False})


class LoginData(pydantic.BaseModel):
    homeserver_url: str
    user_id: str
    password: str
    device_name: str


class Message(pydantic.BaseModel):
    message: str
    rooms: list[str]


class GrafanaStatus(enum.Enum):
    RESOLVED = "resolved"
    FIRING = "firing"


class GrafanaAlertModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(
        alias_generator=lambda x: pydantic.alias_generators.to_camel(x).replace("Url", "URL"),
        populate_by_name=True,
    )


class GrafanaAlert(GrafanaAlertModel):
    status: GrafanaStatus
    labels: dict
    annotations: dict
    starts_at: datetime.datetime
    ends_at: datetime.datetime
    generator_url: str
    fingerprint: str
    silence_url: str
    dashboard_url: str
    panel_url: str
    values: dict | None
    value_string: str | list[dict]

    @pydantic.field_validator("value_string", mode="before")
    @classmethod
    def convert_string(cls, value_str: str | list[dict]) -> pydantic.JsonValue:
        if isinstance(value_str, list):
            return value_str

        def parse_labels(labels_string: str) -> dict[str, str]:
            label_pairs = labels_string.split(", ")

            labels = {}
            for pair in label_pairs:
                label_key, label_value = pair.split("=")
                labels[label_key.strip()] = label_value.strip()

            return labels

        pattern = r"\[([^\]]+)\]"
        matches = re.findall(pattern, value_str)

        result = []
        for match in matches:
            match = match.strip()  # Remove leading/trailing whitespaces
            pair_pattern = r"(\w+)=(?:'(.*?)'|\{(.*?)\}|([\d.\-+eE]+))"
            pairs = re.findall(pair_pattern, match)

            item = {}
            value = None
            for key, var, labels_str, val in pairs:
                if var:
                    value = var
                elif labels_str:
                    value = parse_labels(labels_str)
                elif val:
                    converted_value = float(val)
                    value = int(converted_value) if converted_value.is_integer() else converted_value

                item[key] = value

            result.append(item)

        return result


class GrafanaAlertManagerMessage(GrafanaAlertModel):
    version: int
    group_key: str
    truncated_alerts: int
    status: GrafanaStatus
    receiver: str
    group_labels: dict
    common_labels: dict
    common_annotations: dict
    external_url: str
    alerts: list[GrafanaAlert]


def critical(message: str) -> None:
    logging.critical(message)
    # Need to sigkill as this is the only signal that produces a non-zero exit code.
    # sys.exit(1) does not work as Uvicorn will continue to run.
    os.kill(os.getpid(), signal.SIGKILL)


def exit_on_task_failure(task: asyncio.Task, timeout_ok: bool = False) -> None:
    exception = task.exception()
    is_timeout = isinstance(exception, asyncio.TimeoutError)
    if task.cancelled():
        critical(f"Task {task} was cancelled.")
    if exception and not (timeout_ok and is_timeout):
        # Exception is not a timeout or is a timeout, but a timeout is not accepted
        critical(f"Task {task} failed{', exception message is ' + str(exception) if str(exception) else ''}.")


def format_timedelta(timedelta: datetime.timedelta) -> str:
    days = timedelta.days
    hours, remainder = divmod(timedelta.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    formatted_time = ""
    if days > 0:
        formatted_time += f"{days}d, "
    formatted_time += f"{hours:02}h {minutes:02}m {seconds:02}s"
    return formatted_time


def load_json(path: pathlib.Path, missing_ok: bool = False) -> pydantic.JsonValue:
    if not path.is_file():
        if missing_ok:
            return {}

        raise ValueError(f"JSON file {path} does not exist.")

    with path.open("r") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to load JSON file: {e}") from e


def get_matrix_client() -> nio.AsyncClient:
    client = matrix_client
    if client is None:
        raise RuntimeError("Matrix client is not configured.")
    return client


def create_client(homeserver_url: str, user_id: str) -> nio.AsyncClient:
    client_config = nio.AsyncClientConfig(
        max_limit_exceeded=0,
        max_timeouts=0,
        store_sync_tokens=True,
        encryption_enabled=True,
    )

    return nio.AsyncClient(
        homeserver=homeserver_url,
        user=user_id,
        store_path=cli_args.store_directory,
        config=client_config,
    )


async def token_login(credentials: dict) -> None:
    global matrix_client

    client = create_client(credentials["homeserver-url"], credentials["user-id"])
    matrix_client = client

    try:
        client.restore_login(credentials["user-id"], credentials["device-id"], credentials["access-token"])
    except nio.LocalProtocolError as ex:
        logging.error(f"Failed to log in with access token: {ex}")
        await client.close()

    if client.logged_in:
        logging.info(f"Logged in as {credentials['user-id']} using access token.")


async def initial_login(homeserver_url: str, user_id: str, password: str, device_name: str) -> None:
    global matrix_client

    client = create_client(homeserver_url, user_id)
    matrix_client = client

    try:
        login_response = await client.login(
            password=password,
            device_name=device_name,
        )
    except nio.LocalProtocolError as e:
        # There's an edge case here where the user hasn't installed the correct C
        # dependencies. In that case, a LocalProtocolError is raised on login.
        raise ValueError(
            "Failed to log in. Have you installed the correct dependencies? "
            "https://github.com/poljar/matrix-nio#installation "
            f"Error: {e}",
        ) from e

    # Check if login failed
    if login_response is nio.LoginError:
        raise ValueError(f"Failed to log in: {login_response.message}")

    credentials_path = pathlib.Path(cli_args.credentials_path)

    # Save access token into credentials file
    with credentials_path.open("w") as fh:
        json.dump(
            {
                "homeserver-url": homeserver_url,
                "user-id": user_id,
                "access-token": login_response.access_token,
                "device-id": login_response.device_id,
            },
            fh,
            indent=4,
        )
        logging.info(f"Saved credentials to {cli_args.credentials_path}")
        credentials_path.chmod(0o600)

    logging.info(f"Logged in as {user_id} using password.")


async def verify() -> None:
    client = get_matrix_client()

    async def to_device_callback(event: nio.ToDeviceEvent) -> None:
        # Fetch keys from server to get key verifications stored in the client
        if client.should_query_keys:
            await client.keys_query()

        # matrix-nio currently does not support the new verification flow
        # https://github.com/matrix-nio/matrix-nio/issues/430
        # Workaround from https://github.com/wreald/matrix-nio/commit/5cb8e99965bcb622101b1d6ad6fa86f5a9debb9a
        if event.source.get("type", "") == "m.key.verification.request":
            if not event.sender:
                logging.error("Failed to start verification, request does not contain the sender.")
                return

            content = event.source.get("content")
            if not content:
                logging.error("Failed to start verification, request does not contain the content field.")
                return

            from_device = content.get("from_device")
            if not from_device:
                logging.error("Failed to start verification, request does not contain the other device's name.")
                return

            txid = content.get("transaction_id")
            if not txid:
                logging.error("Failed to start verification, request does not contain a transaction ID.")
                return

            if "m.sas.v1" not in content.get("methods", []):
                logging.error(f"Failed to start verification, {from_device} does not support emoji verification.")
                return

            logging.info(f"Verification request received from {event.sender} [{from_device}].")

            # Send ready event to other device
            verification_ready_msg = nio.ToDeviceMessage(
                type="m.key.verification.ready",
                recipient=event.sender,
                recipient_device=from_device,
                content={
                    "from_device": client.device_id,
                    "methods": ["m.sas.v1"],
                    "transaction_id": txid,
                },
            )

            resp = await client.to_device(verification_ready_msg)
            if isinstance(resp, nio.ToDeviceError):
                logging.error(f"Sending verification ready message failed: {resp}")
                return

        elif isinstance(event, nio.KeyVerificationStart):
            if "emoji" not in event.short_authentication_string:
                logging.error(f"Other device does not support emoji verification {event.short_authentication_string}.")
                return

            resp = await client.accept_key_verification(event.transaction_id)
            if isinstance(resp, nio.ToDeviceError):
                logging.error(f"accept_key_verification failed with {resp}")

            sas = client.key_verifications[event.transaction_id]

            share_key_message = sas.share_key()
            resp = await client.to_device(share_key_message)
            if isinstance(resp, nio.ToDeviceError):
                logging.error(f"to_device failed with {resp}")

        elif isinstance(event, nio.KeyVerificationKey):
            global verification_data
            sas = client.key_verifications[event.transaction_id]
            verification_data = {"emoji": sas.get_emoji(), "transaction-id": event.transaction_id}

        elif isinstance(event, nio.KeyVerificationMac):
            sas = client.key_verifications[event.transaction_id]

            try:
                mac_message = sas.get_mac()
            except nio.LocalProtocolError as e:
                # e.g. it might have been canceled by ourselves
                logging.warning(
                    f"Canceled or protocol error: Reason: {e}.\nVerification with {event.sender} not concluded yet."
                )
            else:
                resp = await client.to_device(mac_message)
                if isinstance(resp, nio.ToDeviceError):
                    logging.error(f"to_device failed with {resp}")

                if sas.verified:
                    logging.info("Emoji verification was successful!")
                elif sas.timed_out:
                    logging.error("Emoji verification failed (timed out).")
                elif sas.canceled:
                    logging.error("Emoji verification failed (canceled).")
                else:
                    logging.error("WTF, this should not be possible to reach after emoji verification.")

        elif isinstance(event, nio.KeyVerificationCancel):
            logging.warning(f"Verification has been canceled by {event.sender} for reason '{event.reason}'.")

        else:
            logging.warning(
                f"Received unexpected ToDeviceEvent {type(event)} [{event.source.get('type', '<unknown type>')}]."
            )

    client.add_to_device_callback(to_device_callback, (nio.ToDeviceEvent,))
    logging.info("Finished setup for verification")


def token_validity_check(
    func: typing.Callable[..., typing.Any],
) -> typing.Callable[..., typing.Any]:
    async def wrapper(*args: str, **kwargs: int) -> None:
        client = get_matrix_client()
        result = await client.whoami()

        if isinstance(result, nio.WhoamiError) and result.status_code == "M_UNKNOWN_TOKEN":
            critical("Access token invalid! Exiting...")

        await func(*args, **kwargs)

    return wrapper


@token_validity_check
async def send_event(event_json: dict, rooms: list[str]) -> None:
    client = get_matrix_client()

    async def send_to_room(room_id: str) -> None:
        while True:
            try:
                await client.room_send(
                    room_id,
                    "m.room.message",
                    content=event_json,
                    ignore_unverified_devices=True,
                )

                return
            except asyncio.CancelledError:
                # Cancellations should be on purpose, don't retry.
                # Re-raise to ensure proper cancellation.
                raise
            except Exception as e:
                logging.error(f"Sending message failed, will retry in 5 seconds: {e}")
                await asyncio.sleep(5)

    tasks = []
    for room in rooms:
        tasks.append(asyncio.create_task(send_to_room(room if room.startswith("!") else room_mappings[room])))

    await asyncio.gather(*tasks)


# This needs to be async because token_validity_check expects an async function.
# This function awaits nothing internally on purpose to not block the event loop.
@token_validity_check
async def finalize_startup() -> None:
    logging.info("Starting sync forever")

    client = get_matrix_client()
    sync_forever_task = asyncio.create_task(client.sync_forever(timeout=30000, full_state=True))
    sync_forever_task.add_done_callback(exit_on_task_failure)

    # Start verification if not verified yet
    if not _is_verified():
        verify_task = asyncio.create_task(verify())
        verify_task.add_done_callback(exit_on_task_failure)


def _is_logged_in() -> bool:
    client = matrix_client
    return client is not None and client.logged_in


def _is_verified() -> bool:
    # The verification process is considered done if at least one device is marked as trusted from our perspective
    # => Verification was run, and we called matrix_client.confirm_short_auth_string()
    client = matrix_client
    return client is not None and any(
        device.verified for device in client.device_store.active_user_devices(client.user_id)
    )


def is_verified() -> None:
    if not _is_verified():
        raise fastapi.HTTPException(409, "Device is not verified.")


def is_not_verified() -> None:
    if _is_verified():
        raise fastapi.HTTPException(409, "Device is already verified.")


def has_verification_data() -> None:
    if not verification_data:
        raise fastapi.HTTPException(409, "Verification data not available.")


def is_logged_in() -> None:
    if not _is_logged_in():
        raise fastapi.HTTPException(409, "Not logged in to matrix.")


def is_not_logged_in() -> None:
    if _is_logged_in():
        raise fastapi.HTTPException(409, "Already logged in to matrix.")


#####################
# FastAPI endpoints #
#####################
@asynccontextmanager
async def lifespan(_: fastapi.FastAPI) -> AsyncGenerator[None, None]:
    store_directory = pathlib.Path(cli_args.store_directory)
    # Create store if it does not exist
    if not store_directory.is_dir():
        store_directory.mkdir(exist_ok=True)

    credentials = load_json(pathlib.Path(cli_args.credentials_path), missing_ok=True)
    if len(credentials) != 0:
        await token_login(credentials)
        await finalize_startup()

    logging.info("FastAPI startup done.")
    yield


app = fastapi.FastAPI(lifespan=lifespan)


@app.exception_handler(fastapi.exceptions.RequestValidationError)
def validation_exception_handler(_: fastapi.Request, exception: fastapi.exceptions.RequestValidationError) -> None:
    logging.error(f"ValidationException: {exception}")


@app.get("/status")
def status() -> dict[str, str]:
    status_id = "not-configured"
    status_msg = "matrix-gateway was not configured with login data yet."

    if _is_logged_in():
        status_id = "not-verified"
        status_msg = "matrix-gateway was configured with login data, but verification did not yet happen."

    if _is_verified():
        status_id = "ready"
        status_msg = "matrix-gateway is ready to send events :)"

    return {"status": status_id, "message": status_msg}


@app.post("/login", dependencies=[fastapi.Depends(is_not_logged_in)])
async def login(login_data: LoginData) -> None:
    await initial_login(login_data.homeserver_url, login_data.user_id, login_data.password, login_data.device_name)
    await finalize_startup()


@app.get(
    "/emoji",
    dependencies=[
        fastapi.Depends(is_logged_in),
        fastapi.Depends(is_not_verified),
        fastapi.Depends(has_verification_data),
    ],
)
def show_emoji() -> list[tuple[str, str]]:
    return verification_data["emoji"]


@app.post("/verify", dependencies=[fastapi.Depends(is_logged_in), fastapi.Depends(is_not_verified)])
async def verification() -> None:
    if not verification_data:
        raise fastapi.HTTPException(409, "Verification data not available.")

    client = get_matrix_client()

    # Verify that emojis match
    resp = await client.confirm_short_auth_string(verification_data["transaction-id"])
    if isinstance(resp, nio.ToDeviceError):
        confirm_short_auth_string_failed_message = f"confirm_short_auth_string failed with {resp}"
        logging.error(confirm_short_auth_string_failed_message)
        raise fastapi.HTTPException(400, confirm_short_auth_string_failed_message)

    # Tell the other device we are done
    sas = client.key_verifications[verification_data["transaction-id"]]
    done_message = nio.ToDeviceMessage(
        type="m.key.verification.done",
        recipient=sas.other_olm_device.user_id,
        recipient_device=sas.other_olm_device.device_id,
        content={"transaction_id": sas.transaction_id},
    )

    resp = await client.to_device(done_message)
    if isinstance(resp, nio.ToDeviceError):
        logging.error(f"Sending verification done message failed: {resp}")
        return


logging.info("Confirmed emoji verification.")


@app.post("/send-text", dependencies=[fastapi.Depends(is_logged_in), fastapi.Depends(is_verified)])
async def send_text(message: Message) -> None:
    event_json = {"msgtype": "m.text", "body": message.message}
    await send_event(event_json, message.rooms)


@app.post("/send-markdown", dependencies=[fastapi.Depends(is_logged_in), fastapi.Depends(is_verified)])
async def send_markdown(message: Message) -> None:
    event_json = {
        "msgtype": "m.text",
        "body": message.message,
        "format": "org.matrix.custom.html",
        "formatted_body": markdown.markdown(message.message, extensions=["fenced_code", "nl2br"]),
    }
    await send_event(event_json, message.rooms)


def esc(value: str) -> str:
    return html.escape(value, quote=True)


def linkify(value: str) -> str:
    text = str(value)
    parts = []
    end = 0
    for match in linkifier.match(text) or []:
        if match.schema not in {"http:", "https:"}:
            continue

        parts.append(esc(text[end : match.index]))
        parts.append(f'<a href="{esc(match.url)}">{esc(match.raw)}</a>')
        end = match.last_index

    parts.append(esc(text[end:]))
    return "".join(parts)


def visible_labels(labels: dict) -> dict:
    return {key: value for key, value in labels.items() if key not in HIDDEN_LABELS and not str(key).startswith("__")}


def keep_alert_conditional(pair: tuple[str, typing.Any]) -> bool:
    key = str(pair[0])
    return not key.endswith("_ignore") and not re.match(r"^[A-Z]$", key)


def format_alert_value(value: str | float) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def html_list(items: dict) -> str:
    result = ["<ul>"]
    for key, value in items.items():
        result.append(f"<li><b>{esc(key)}</b>: {linkify(format_alert_value(value))}</li>")
    result.append("</ul>")

    return "".join(result)

def alert_headline(alert: GrafanaAlert, common_labels: dict) -> tuple[str, set[str]]:
    labels = visible_labels(alert.labels or {})

    shown = {"alertname"}
    parts = [str(labels.get("alertname", "Unknown alert"))]

    if labels.get("instance"):
        parts.append(str(labels["instance"]))
        shown.add("instance")

    for key, value in labels.items():
        if key not in shown and common_labels.get(key) != value:
            parts.append(f"{key}={value}")
            shown.add(key)

    return " · ".join(parts), shown


def alert_values(alert: GrafanaAlert) -> dict:
    values = {key: value for key, value in (alert.values or {}).items() if keep_alert_conditional((key, value))}
    if values:
        return values

    values = {}
    for entry in alert.value_string or []:
        if not isinstance(entry, dict):
            continue

        var = entry.get("var")
        value = entry.get("value")
        if var is not None and value is not None and keep_alert_conditional((var, value)):
            values[str(var)] = value

    return values


def alert_condition_labels(alert: GrafanaAlert) -> list[dict]:
    alert_labels = alert.labels or {}
    condition_labels = []
    for entry in alert.value_string or []:
        labels = entry.get("labels")
        if not isinstance(labels, dict):
            continue

        extra_labels = {
            key: value
            for key, value in labels.items()
            if key not in HIDDEN_LABELS and not str(key).startswith("__") and alert_labels.get(key) != value
        }
        if extra_labels:
            condition_labels.append(extra_labels)

    return condition_labels


def is_no_data_alert(alert: GrafanaAlert) -> bool:
    return alert.status == GrafanaStatus.FIRING and (alert.labels or {}).get("alertname") == "DatasourceNoData"


def alert_headline_common_labels(alerts: list[GrafanaAlert]) -> list[dict]:
    labels_by_alert = [visible_labels(alert.labels or {}) for alert in alerts]
    indices_by_alertname = {}
    common_labels_by_index = {}

    for index, labels in enumerate(labels_by_alert):
        alertname = labels.get("alertname")
        if alertname:
            indices_by_alertname.setdefault(alertname, []).append(index)
        else:
            common_labels_by_index[index] = labels

    for indices in indices_by_alertname.values():
        peer_labels = [labels_by_alert[index] for index in indices]
        common_labels = {
            key: value
            for key, value in peer_labels[0].items()
            if all(key in labels and labels[key] == value for labels in peer_labels[1:])
        }
        for index in indices:
            common_labels_by_index[index] = common_labels

    return [common_labels_by_index[index] for index in range(len(alerts))]


def render_grafana_alert(
    alert: GrafanaAlert,
    common_labels: dict,
) -> tuple[str, str]:
    is_no_data = is_no_data_alert(alert)
    status_text = alert.status.value
    if is_no_data:
        emoji = "❓"
        status_text = "no data"
    elif alert.status == GrafanaStatus.FIRING:
        emoji =  "🔥"
    else:
        emoji = "✅"

    headline, shown_labels = alert_headline(alert, common_labels)
    summary = (alert.annotations or {}).get("summary", "")

    if alert.status == GrafanaStatus.FIRING:
        when = f"since {alert.starts_at.strftime('%Y-%m-%d %H:%M')}"
    else:
        when = (
            f"from {alert.starts_at.strftime('%Y-%m-%d %H:%M')} to {alert.ends_at.strftime('%Y-%m-%d %H:%M')} "
            f"({format_timedelta(alert.ends_at - alert.starts_at)})"
        )

    labels = visible_labels(alert.labels or {})
    extra_labels = {key: value for key, value in labels.items() if key not in shown_labels}
    other_annotations = {
        key: value for key, value in (alert.annotations or {}).items() if key not in {"summary", "headline"}
    }

    body = []
    if summary:
        body.append(f"<p>{linkify(summary)}</p>")
    body.append(f"<p><i>{esc(status_text)} {esc(when)}</i></p>")

    values = alert_values(alert)
    if values:
        body.append(f"<p><b>Values</b></p>{html_list(values)}")

    if extra_labels:
        body.append(f"<p><b>Labels</b></p>{html_list(extra_labels)}")

    condition_labels = alert_condition_labels(alert)
    if condition_labels:
        body.append("<p><b>Condition labels</b></p>")
        body.extend(html_list(labels_group) for labels_group in condition_labels)

    if other_annotations:
        body.append(f"<p><b>Annotations</b></p>{html_list(other_annotations)}")

    links = []
    for name, url in (
        ("Silence", alert.silence_url),
        ("Dashboard", alert.dashboard_url),
        ("Panel", alert.panel_url),
        ("Source", alert.generator_url),
    ):
        if url:
            links.append(f'<a href="{esc(url)}">{name}</a>')
    if links:
        body.append(f"<p>{' · '.join(links)}</p>")

    formatted = f"<details><summary>{emoji} <b>{linkify(headline)}</b></summary>{''.join(body)}</details>"
    plain = f"{emoji} {headline}" + (f" — {summary}" if summary else "")
    return plain, formatted


@app.post("/send-file", dependencies=[fastapi.Depends(is_logged_in), fastapi.Depends(is_verified)])
async def send_file(file: fastapi.UploadFile, rooms: list[str]) -> None:
    if file.content_type is None:
        logging.info(f"Received file {file.filename} with no content type.")
        raise fastapi.HTTPException(400, "File has no content type.")

    file_data = await file.read()

    # Upload file to matrix homeserver
    client = get_matrix_client()
    upload_response, decryption_keys = await client.upload(
        lambda x, y: file_data, content_type=file.content_type, filename=file.filename, filesize=file.size, encrypt=True
    )

    # Check status of upload
    if isinstance(upload_response, nio.UploadResponse):
        logging.info(f"Uploaded file {file.filename} to matrix server.")
    else:
        error_message = f"Failed to upload file {file.filename} to matrix server: {upload_response}"
        logging.info(error_message)
        raise fastapi.HTTPException(502, error_message)

    if file.content_type.startswith("image/"):
        msgtype = "m.image"
    elif file.content_type.startswith("audio/"):
        msgtype = "m.audio"
    elif file.content_type.startswith("video/"):
        msgtype = "m.video"
    else:
        msgtype = "m.file"

    event_json = {
        "msgtype": msgtype,
        "body": file.filename,
        "info": {"size": file.size, "mimetype": file.content_type},
        "file": {"url": upload_response.content_uri} | decryption_keys,
    }

    if file.content_type.startswith("image/"):
        # Get image metadata
        if file.content_type.startswith("image/svg"):
            width = 100
            height = 100
        else:
            image = Image.open(io.BytesIO(file_data))
            width = image.width
            height = image.height

        event_json["info"] |= {"w": width, "h": height}

    await send_event(event_json, rooms)


@app.post("/grafana-alerts", dependencies=[fastapi.Depends(is_logged_in), fastapi.Depends(is_verified)])
async def grafana_alerts(data: GrafanaAlertManagerMessage) -> None:
    headline_common_labels = alert_headline_common_labels(data.alerts)
    alerts = [
        render_grafana_alert(alert, common_labels) for alert, common_labels in zip(data.alerts, headline_common_labels, strict=False)
    ]
    no_data_count = sum(is_no_data_alert(alert) for alert in data.alerts)
    firing_count = sum(alert.status == GrafanaStatus.FIRING and not is_no_data_alert(alert) for alert in data.alerts)
    resolved_count = sum(alert.status == GrafanaStatus.RESOLVED for alert in data.alerts)

    header_parts = []
    if firing_count:
        header_parts.append(f"🔥 {firing_count} firing")
    if no_data_count:
        header_parts.append(f"❓ {no_data_count} no data")
    if resolved_count:
        header_parts.append(f"✅ {resolved_count} resolved")

    common_labels = visible_labels(data.common_labels or {})
    header = " · ".join(header_parts) or "Grafana alerts"
    if common_labels.get("alertname"):
        header += f" — {common_labels['alertname']}"
    if len(data.alerts) > 1:
        other_common_labels = {key: value for key, value in common_labels.items() if key != "alertname"}
        if other_common_labels:
            header += " · " + " · ".join(f"{key}={value}" for key, value in other_common_labels.items())

    plain_parts = []
    html_parts = []
    if data.truncated_alerts:
        warning = f"⚠️ {data.truncated_alerts} {inflector.plural('alert', data.truncated_alerts)} were truncated."
        plain_parts.append(warning)
        html_parts.append(f"<p>{esc(warning)}</p>")

    formatted_header = f"<h4>{linkify(header)}</h4>"
    formatted_length = len(formatted_header) + sum(len(part) for part in html_parts)
    included_alerts = 0
    for plain_alert, formatted_alert in alerts:
        if html_parts and formatted_length + len(formatted_alert) > MAX_FORMATTED_LENGTH:
            break

        plain_parts.append(plain_alert)
        html_parts.append(formatted_alert)
        formatted_length += len(formatted_alert)
        included_alerts += 1

    omitted_alerts = len(data.alerts) - included_alerts
    if omitted_alerts:
        omitted_message = f"… and {omitted_alerts} more {inflector.plural('alert', omitted_alerts)} omitted."
        plain_parts.append(omitted_message)
        html_parts.append(f"<p>{esc(omitted_message)}</p>")

    event_json = {
        "msgtype": "m.text",
        "body": header + ("\n" + "\n".join(plain_parts) if plain_parts else ""),
        "format": "org.matrix.custom.html",
        "formatted_body": f"{formatted_header}{''.join(html_parts)}",
    }
    await send_event(event_json, [room_mappings["peet"]])


# Init code that has to be run on import of the script by uvicorn as well
parser = argparse.ArgumentParser(description="Runs a matrix gateway server.")
parser.add_argument("--verbose", "-v", action="store_true", help="Show info log lines.")
parser.add_argument("--port", "-p", type=int, required=True, help="The port to listen on.")
parser.add_argument(
    "--store",
    "-s",
    dest="store_directory",
    required=True,
    help="The path to the matrix client store directory.",
)
parser.add_argument(
    "--credentials",
    "-c",
    dest="credentials_path",
    required=True,
    help="The path to store the matrix login credentials at.",
)
cli_args = parser.parse_args()

if __name__ == "__main__":
    # Should only be run on script execution, otherwise uvicorn starts itself
    loglevel = logging.INFO if cli_args.verbose else logging.WARNING
    logging.basicConfig(level=loglevel)
    uvicorn.run("matrix-gateway:app", loop="asyncio", port=cli_args.port, log_level=loglevel)
