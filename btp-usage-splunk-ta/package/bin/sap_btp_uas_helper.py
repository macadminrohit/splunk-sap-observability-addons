import json
import logging

import import_declare_test  # noqa: F401  (sets up sys.path for bundled libs)
import certifi
from solnlib import conf_manager, log
from splunklib import modularinput as smi

import uas_client

ADDON_NAME = "ta_sap_btp_uas"
ACCOUNT_CONF = "ta_sap_btp_uas_account"


def logger_for_input(input_name: str) -> logging.Logger:
    return log.Logs().get_logger(f"{ADDON_NAME.lower()}_{input_name}")


def get_account(session_key: str, account_name: str):
    # The client secret is stored encrypted in Splunk's credential store (passwords.conf);
    # solnlib decrypts it here using the session key. It is never written to inputs.conf.
    cfm = conf_manager.ConfManager(
        session_key,
        ADDON_NAME,
        realm=f"__REST_CREDENTIAL__#{ADDON_NAME}#configs/conf-{ACCOUNT_CONF}",
    )
    return cfm.get_conf(ACCOUNT_CONF).get(account_name)


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
                    conf_name="ta_sap_btp_uas_settings",
                )
            )
            log.modular_input_start(logger, normalized_input_name)

            account_name = input_item.get("account")
            account = get_account(session_key, account_name)
            client = uas_client.UasClient(
                token_url=account.get("token_url"),
                reporting_url=account.get("reporting_url"),
                client_id=account.get("client_id"),
                client_secret=account.get("client_secret"),
                ca_file=certifi.where(),
                logger=logger,
            )
            endpoints = [
                e.strip()
                for e in (input_item.get("endpoints") or "monthlyUsage,monthlySubaccountsCost").split(",")
                if e.strip()
            ]
            months_back = int(input_item.get("months_back") or 2)
            events = uas_client.collect(
                client,
                account=account_name,
                endpoints=endpoints,
                months_back=months_back,
                logger=logger,
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
                logger, input_name, "sap:btp:uas:*", len(events), index, account=account_name
            )
            log.modular_input_end(logger, normalized_input_name)
        except uas_client.UasAuthError as exc:
            log.log_exception(logger, exc, "sap_btp_uas_auth_error",
                              msg_before="UAS authentication/authorisation failed (check the service key and the reporting plan): ")
        except Exception as exc:  # noqa: BLE001
            log.log_exception(logger, exc, "sap_btp_uas_error",
                              msg_before="Exception raised while polling SAP BTP usage API: ")
