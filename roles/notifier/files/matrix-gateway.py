#!/usr/bin/env python3

import argparse
import asyncio
import datetime
import enum
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
from PIL import Image

INDENT = "\u00a0\u00a0\u00a0\u00a0"

matrix_client: nio.AsyncClient | None = None
verification_data: dict | None = None

room_mappings = {
    "peet": "!yQKueemwhhtwODXBHs:matrix.org",
}

inflector = inflect.engine()


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

    # noinspection PyNestedDecorators
    # https://youtrack.jetbrains.com/issue/PY-34368/False-warning-This-decorator-will-not-receive-a-callable-it-may-expect-when-classmethod-is-not-the-last-applied
    @pydantic.field_validator("value_string", mode="before")
    @classmethod
    def convert_string(cls, value_str: str) -> pydantic.JsonValue:
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
    # sys.exit(1) does not work as uvicorn will continue to run.
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

    matrix_client = create_client(credentials["homeserver-url"], credentials["user-id"])

    try:
        matrix_client.restore_login(credentials["user-id"], credentials["device-id"], credentials["access-token"])
    except nio.LocalProtocolError as ex:
        logging.error(f"Failed to login with access token: {ex}")
        await matrix_client.close()

    if matrix_client.logged_in:
        logging.info(f"Logged in as {credentials['user-id']} using access token.")


async def initial_login(homeserver_url: str, user_id: str, password: str, device_name: str) -> None:
    global matrix_client

    matrix_client = create_client(homeserver_url, user_id)

    try:
        login_response = await matrix_client.login(
            password=password,
            device_name=device_name,
        )
    except nio.LocalProtocolError as e:
        # There's an edge case here where the user hasn't installed the correct C
        # dependencies. In that case, a LocalProtocolError is raised on login.
        raise ValueError(
            "Failed to login. Have you installed the correct dependencies? "
            "https://github.com/poljar/matrix-nio#installation "
            f"Error: {e}",
        ) from e

    # Check if login failed
    if login_response is nio.LoginError:
        raise ValueError(f"Failed to login: {login_response.message}")

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
    async def to_device_callback(event: nio.ToDeviceEvent) -> None:
        # Fetch keys from server to get key verifications stored in the client
        if matrix_client.should_query_keys:
            await matrix_client.keys_query()

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
                    "from_device": matrix_client.device_id,
                    "methods": ["m.sas.v1"],
                    "transaction_id": txid,
                },
            )

            resp = await matrix_client.to_device(verification_ready_msg)
            if isinstance(resp, nio.ToDeviceError):
                logging.error(f"Sending verification ready message failed: {resp}")
                return

        elif isinstance(event, nio.KeyVerificationStart):
            if "emoji" not in event.short_authentication_string:
                logging.error(f"Other device does not support emoji verification {event.short_authentication_string}.")
                return

            resp = await matrix_client.accept_key_verification(event.transaction_id)
            if isinstance(resp, nio.ToDeviceError):
                logging.error(f"accept_key_verification failed with {resp}")

            sas = matrix_client.key_verifications[event.transaction_id]

            share_key_message = sas.share_key()
            resp = await matrix_client.to_device(share_key_message)
            if isinstance(resp, nio.ToDeviceError):
                logging.error(f"to_device failed with {resp}")

        elif isinstance(event, nio.KeyVerificationKey):
            global verification_data
            sas = matrix_client.key_verifications[event.transaction_id]
            verification_data = {"emoji": sas.get_emoji(), "transaction-id": event.transaction_id}

        elif isinstance(event, nio.KeyVerificationMac):
            sas = matrix_client.key_verifications[event.transaction_id]

            try:
                mac_message = sas.get_mac()
            except nio.LocalProtocolError as e:
                # e.g. it might have been cancelled by ourselves
                logging.warning(
                    f"Cancelled or protocol error: Reason: {e}.\nVerification with {event.sender} not concluded yet."
                )
            else:
                resp = await matrix_client.to_device(mac_message)
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
            logging.warning(f"Verification has been cancelled by {event.sender} for reason '{event.reason}'.")

        else:
            logging.warning(
                f"Received unexpected ToDeviceEvent {type(event)} [{event.source.get('type', '<unknown type>')}]."
            )

    matrix_client.add_to_device_callback(to_device_callback, (nio.ToDeviceEvent,))
    logging.info("Finished setup for verification")


def token_validity_check(
    func: typing.Callable[..., typing.Any],
) -> typing.Callable[..., typing.Any]:
    async def wrapper(*args: str, **kwargs: int) -> None:
        result = await matrix_client.whoami()

        if isinstance(result, nio.WhoamiError) and result.status_code == "M_UNKNOWN_TOKEN":
            critical("Access token invalid! Exiting...")

        await func(*args, **kwargs)

    return wrapper


@token_validity_check
async def send_event(event_json: dict, rooms: list[str]) -> None:
    async def send_to_room(room_id: str) -> None:
        while True:
            try:
                await matrix_client.room_send(
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

    sync_forever_task = asyncio.create_task(matrix_client.sync_forever(timeout=30000, full_state=True))
    sync_forever_task.add_done_callback(exit_on_task_failure)

    # Start verification if not verified yet
    if not _is_verified():
        verify_task = asyncio.create_task(verify())
        verify_task.add_done_callback(exit_on_task_failure)


def _is_logged_in() -> bool:
    return matrix_client and matrix_client.logged_in


def _is_verified() -> bool:
    # The verification process is considered done if at least one device is marked as trusted from our perspective
    # => Verification was run, and we called matrix_client.confirm_short_auth_string()
    return matrix_client and any(
        device.verified for device in matrix_client.device_store.active_user_devices(matrix_client.user_id)
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

    # Verify that emojis match
    resp = await matrix_client.confirm_short_auth_string(verification_data["transaction-id"])
    if isinstance(resp, nio.ToDeviceError):
        confirm_short_auth_string_failed_message = f"confirm_short_auth_string failed with {resp}"
        logging.error(confirm_short_auth_string_failed_message)
        raise fastapi.HTTPException(400, confirm_short_auth_string_failed_message)

    # Tell the other device we are done
    sas = matrix_client.key_verifications[verification_data["transaction-id"]]
    done_message = nio.ToDeviceMessage(
        type="m.key.verification.done",
        recipient=sas.other_olm_device.user_id,
        recipient_device=sas.other_olm_device.device_id,
        content={"transaction_id": sas.transaction_id},
    )

    resp = await matrix_client.to_device(done_message)
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


@app.post("/send-file", dependencies=[fastapi.Depends(is_logged_in), fastapi.Depends(is_verified)])
async def send_file(file: fastapi.UploadFile, rooms: list[str]) -> None:
    file_data = await file.read()

    # Upload file to matrix homeserver
    upload_response, decryption_keys = await matrix_client.upload(
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
    grafana_alert_message = ""

    if data.truncated_alerts:
        grafana_alert_message += (
            f"Warning: {data.truncated_alerts} {inflector.plural('alert', data.truncated_alerts)} were truncated.\n\n"
        )

    alert_strings = []
    for alert in data.alerts:
        if (
            alert.status == GrafanaStatus.FIRING
            and alert.labels
            and alert.labels.get("alertname") == "DatasourceNoData"
        ):
            alert_status_value = "no data"
        else:
            alert_status_value = alert.status.value

        status_emoji = ""
        if alert.status == GrafanaStatus.FIRING:
            status_emoji = " ❓" if alert.labels and alert.labels.get("alertname") == "DatasourceNoData" else " 🔥"
        elif alert.status == GrafanaStatus.RESOLVED:
            status_emoji = " ✅"

        alert_string = f"Status: {alert_status_value}{status_emoji}\n"

        if alert.status == GrafanaStatus.FIRING:
            alert_string += f"Since: {alert.starts_at.strftime('%c')}\n"
        elif alert.status == GrafanaStatus.RESOLVED:
            alert_string += (
                f"Was firing from {alert.starts_at.strftime('%c')} to {alert.ends_at.strftime('%c')}"
                f" ({format_timedelta(alert.ends_at - alert.starts_at)})\n"
            )

        metric_names = set()
        if alert.value_string:
            for value in alert.value_string:
                metric_name_from_value = value.get("metric")
                if not metric_name_from_value:
                    # Sometimes, there is no metric name in the labels for some reason. Don't add anything then.
                    metric_name_from_label = value.get("labels", {}).get("__name__")
                    if metric_name_from_label:
                        metric_names.add(metric_name_from_label)
                else:
                    metric_names.add(metric_name_from_value)
        metric_names_str = "', '".join(metric_names)

        value_labels = {}
        if alert.value_string:
            for value in alert.value_string:
                labels = value.get("labels")
                if not labels:
                    continue

                if isinstance(labels, dict):
                    value_labels[str(list(value.get("labels").keys()))] = labels
                else:
                    value_labels[str(labels)] = labels

        alert_string += "Caused by"
        if metric_names_str:
            alert_string += f" metric '{metric_names_str}' with"
        if alert.values or alert.value_string:
            alert_string += " values"
        alert_string += ":"

        def keep_alert_conditional(pair: tuple[str, typing.Any]) -> bool:
            return not pair[0].endswith("_ignore") and not re.match(r"^[A-Z]$", pair[0])

        if alert.values:
            filtered_values = filter(keep_alert_conditional, alert.values.items())
            alert_string += f"\n{INDENT}- "
            alert_string += f"\n{INDENT}- ".join(f"{key}: {value}" for key, value in filtered_values)
        elif alert.value_string:
            for entry in alert.value_string:
                if (
                    not entry.get("var")
                    or not entry.get("value")
                    or not keep_alert_conditional((entry.get("var"), entry.get("value")))
                ):
                    continue

                alert_string += (
                    f"\n{INDENT}- {entry.get('var', '&lt;unknown var name&gt;')}: {entry.get('value', 'N/A')}"
                )
        alert_string += "\n"

        if value_labels:
            for labels in value_labels.values():
                alert_string += "\nAlert Condition Labels:\n"

                if isinstance(labels, dict):
                    filtered_values = filter(lambda pair: not pair[0].startswith("__"), labels.items())
                    alert_string += f"{INDENT}- "
                    alert_string += f"\n{INDENT}- ".join(f"{key}: {value}" for key, value in filtered_values)
                else:
                    alert_string += f"{INDENT}- {labels}\n"

                alert_string += "\n"

        if alert.labels:
            alert_string += f"\nAlert Labels:\n{INDENT}- "
            alert_string += f"\n{INDENT}- ".join(f"{key}: {value}" for key, value in alert.labels.items())
            alert_string += "\n"

        if alert.annotations:
            alert_string += f"\nAlert Annotations: \n{INDENT}- "
            alert_string += f"\n{INDENT}- ".join(f"{key}: {value}" for key, value in alert.annotations.items())
            alert_string += "\n"

        alert_string += f"\n[Silence]({alert.silence_url})"

        # Escape markdown chars
        alert_string = alert_string.replace("__", "\\_\\_").replace("**", "\\*\\*").replace("#", "\\#")

        if alert.generator_url:
            alert_string += f", [Generator]({alert.generator_url})"

        if alert.dashboard_url:
            alert_string += f", [Dashboard]({alert.dashboard_url})"

        if alert.panel_url:
            alert_string += f", [Panel]({alert.panel_url})"

        alert_string += "\n"
        alert_strings.append(alert_string)

    alert_strings_joined = "\n--\n\n".join(alert_strings)
    if len(alert_strings_joined) > 15000:
        alert_strings_joined = alert_strings_joined[:15000] + "\n\n**Message too long, truncated!**"

    grafana_alert_message = "#### Messages from Grafana\n\n"
    grafana_alert_message += alert_strings_joined
    await send_markdown(Message(message=grafana_alert_message, rooms=[room_mappings["peet"]]))


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
