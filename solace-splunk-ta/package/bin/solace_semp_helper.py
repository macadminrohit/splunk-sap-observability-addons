import json
import logging
import urllib.parse

import import_declare_test  # noqa: F401  (sets up sys.path for bundled libs)
import certifi
from solnlib import conf_manager, log
from splunklib import modularinput as smi

import semp_client

ADDON_NAME = "ta_solace_semp"
ACCOUNT_CONF = "ta_solace_semp_account"


def logger_for_input(input_name: str) -> logging.Logger:
    return log.Logs().get_logger(f"{ADDON_NAME.lower()}_{input_name}")


def get_account(session_key: str, account_name: str):
    # Password is stored encrypted in Splunk's credential store (passwords.conf);
    # solnlib decrypts it here using the session key. It is never written to inputs.conf.
    cfm = conf_manager.ConfManager(
        session_key,
        ADDON_NAME,
        realm=f"__REST_CREDENTIAL__#{ADDON_NAME}#configs/conf-{ACCOUNT_CONF}",
    )
    return cfm.get_conf(ACCOUNT_CONF).get(account_name)


def _as_int(value, default):
    try:
        n = int(str(value).strip())
        return n if n >= 0 else default
    except (TypeError, ValueError):
        return default


def _as_bool(value, default):
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def validate_input(definition: smi.ValidationDefinition):
    return


def stream_events(inputs: smi.InputDefinition, event_writer: smi.EventWriter):
    for input_name, input_item in inputs.inputs.items():
        normalized_input_name = input_name.split("/")[-1]
        logger = logger_for_input(normalized_input_name)
        try:
            session_key = inputs.metadata["session_key"]
            logger.setLevel(
                conf_manager.get_log_level(
                    logger=logger,
                    session_key=session_key,
                    app_name=ADDON_NAME,
                    conf_name="ta_solace_semp_settings",
                )
            )
            log.modular_input_start(logger, normalized_input_name)

            account_name = input_item.get("account")
            account = get_account(session_key, account_name)
            client = semp_client.SempClient(
                base_url=account.get("semp_url"),
                username=account.get("username"),
                password=account.get("password"),
                ca_file=certifi.where(),
                logger=logger,
            )
            object_types = [
                t.strip()
                for t in (input_item.get("object_types") or "msgVpn,queue").split(",")
                if t.strip()
            ]
            events = semp_client.collect(
                client,
                broker=account_name,
                object_types=object_types,
                vpn_filter=input_item.get("msg_vpn_filter") or "*",
                name_filter=input_item.get("name_filter") or "*",
                logger=logger,
                max_items=_as_int(input_item.get("max_items"), semp_client.DEFAULT_MAX_ITEMS),
                trim_config=_as_bool(input_item.get("trim_config_fields"), True),
            )
            index = input_item.get("index")
            for sourcetype, event in events:
                event_writer.write_event(
                    smi.Event(
                        data=json.dumps(event, ensure_ascii=False, default=str),
                        index=index,
                        host=client.host,
                        sourcetype=sourcetype,
                    )
                )
            log.events_ingested(
                logger, input_name, "solace:semp:*", len(events), index, account=account_name
            )
            log.modular_input_end(logger, normalized_input_name)
        except semp_client.SempAuthError as exc:
            log.log_exception(logger, exc, "solace_semp_auth_error",
                              msg_before="SEMP authentication/authorisation failed (check the read-only user): ")
        except Exception as exc:  # noqa: BLE001
            log.log_exception(logger, exc, "solace_semp_error",
                              msg_before="Exception raised while polling Solace SEMP: ")
