"""Paid native launch preparation without FLE prototype enumeration shortcuts."""
from ..iteration_timing import decode_native
from types import SimpleNamespace

from ..launch_readiness import validate
from ..telemetry import phase


def execute(native, action: str, parameters: dict, trace=None) -> str:
    validate(action, parameters)
    native.require_launch_reconciliation()
    if action == 'factory_launch_pad':
        with phase('entity_lookup', trace):
            target = decode_native(native.call('prepare_launch_pad', parameters))
        with phase('approach', trace):
            native.backend._fair.approach(SimpleNamespace(**target['position']), target['name'])
        with phase('transfer_rpc', trace):
            native.call('build_launch_pad', parameters)
        return 'Paid landing pad returned; receipt and native identity require verification'
    if action == 'factory_launch_payload':
        with phase('approach', trace):
            native.approach_role(parameters['role'])
        with phase('transfer_rpc', trace):
            native.call('load_launch_payload', parameters)
        return 'Payload transfer returned; exact rocket cargo and receipt require verification'
    with phase('transfer_rpc', trace):
        native.call('begin_launch_fish', parameters)
    native.backend._fair.wait(timeout=30)
    return 'Native fish mining returned; mined-entity receipt and real inventory require verification'
