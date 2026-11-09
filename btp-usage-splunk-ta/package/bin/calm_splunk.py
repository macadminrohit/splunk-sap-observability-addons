"""Splunk-side glue shared by the two Cloud ALM inputs (not imported by unit tests)."""
import json
import logging

import certifi
from solnlib import conf_manager, log
from splunklib import modularinput as smi

import calm_client

ADDON_NAME = "ta_sap_btp_uas"
ACCOUNT_CONF = "ta_sap_btp_uas_calm_account"


def logger_for_input(input_name: str) -> logging.Logger:
    return log.Logs().get_logger(f"{ADDON_NAME.lower()}_{input_name}")


def get_account(session_key: str, account_name: str):
    # The client secret is stored encrypted in Splunk's credential store (passwords.conf);
    # solnlib decrypts it here with the session key. It is never written to inputs.conf.
    cfm = conf_manager.ConfManager(
        session_key,
        ADDON_NAME,
        realm=f"__REST_CREDENTIAL__#{ADDON_NAME}#configs/conf-{ACCOUNT_CONF}",
    )
    return cfm.get_conf(ACCOUNT_CONF).get(account_name)


def run(inputs: smi.InputDefinition, event_writer: smi.EventWriter, kind: str, produce):
    """Common loop. ``produce(client, account_name, input_item, logger)`` -> [(sourcetype, event)]."""
    for input_name, input_item in inputs.inputs.items():
        normalized = input_name.split("/")[-1]
        logger = logger_for_input(normalized)
        try:
            session_key = inputs.metadata["session_key"]
            logger.setLevel(conf_manager.get_log_level(
                logger=logger, session_key=session_key, app_name=ADDON_NAME,
                conf_name="ta_sap_btp_uas_settings"))
            log.modular_input_start(logger, normalized)
            account_name = input_item.get("account")
            account = get_account(session_key, account_name)
            client = calm_client.CalmClient(
                token_url=account.get("token_url"),
                api_url=account.get("api_url"),
                client_id=account.get("client_id"),
                client_secret=account.get("client_secret"),
                ca_file=certifi.where(),
                logger=logger,
            )
            events = produce(client, account_name, input_item, logger)
            index = input_item.get("index")
            for sourcetype, event in events:
                event_writer.write_event(smi.Event(
                    data=json.dumps(event, ensure_ascii=False, default=str),
                    index=index, host=client.host, sourcetype=sourcetype))
            log.events_ingested(logger, input_name, "sap:calm:*", len(events), index,
                                account=account_name)
            log.modular_input_end(logger, normalized)
        except calm_client.UasAuthError as exc:
            log.log_exception(logger, exc, f"{kind}_auth_error",
                              msg_before="Cloud ALM authentication/authorisation failed "
                                         "(check the service key and its calm-api.* scopes): ")
        except Exception as exc:  # noqa: BLE001
            log.log_exception(logger, exc, f"{kind}_error",
                              msg_before="Exception raised while polling SAP Cloud ALM: ")
