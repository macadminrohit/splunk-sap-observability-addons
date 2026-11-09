import import_declare_test  # noqa: F401  (sets up sys.path for bundled libs)
from splunklib import modularinput as smi

import calm_client
import calm_splunk


def validate_input(definition: smi.ValidationDefinition):
    return


def _produce(client, account_name, input_item, logger):
    return calm_client.collect_catalogue(client, account_name, include_dimensions=True, logger=logger)


def stream_events(inputs: smi.InputDefinition, event_writer: smi.EventWriter):
    calm_splunk.run(inputs, event_writer, "sap_calm_catalogue", _produce)
