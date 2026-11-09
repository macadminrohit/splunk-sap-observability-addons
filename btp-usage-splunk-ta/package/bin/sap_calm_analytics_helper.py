import import_declare_test  # noqa: F401  (sets up sys.path for bundled libs)
from splunklib import modularinput as smi

import calm_client
import calm_splunk


def validate_input(definition: smi.ValidationDefinition):
    # Reject malformed query JSON at save time instead of failing on every poll.
    calm_client.parse_queries(definition.parameters.get("queries"))


def _produce(client, account_name, input_item, logger):
    queries = calm_client.parse_queries(input_item.get("queries"))
    return calm_client.collect_data(
        client,
        account_name,
        queries,
        time_range=input_item.get("time_range") or "L1H",
        fmt=input_item.get("format") or "table",
        table_type=input_item.get("table_type") or "last",
        resolution=(input_item.get("resolution") or "").strip() or None,
        page_size=int(input_item.get("page_size") or 500),
        logger=logger,
    )


def stream_events(inputs: smi.InputDefinition, event_writer: smi.EventWriter):
    calm_splunk.run(inputs, event_writer, "sap_calm_analytics", _produce)
