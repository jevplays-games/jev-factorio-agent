"""Forward compatible edges must not be mistaken for backward cycles.

Uses recorded consumed epoch facts; signature transport is mocked explicitly.
Actual enrolled-signature replay is qualified separately on Linux.
"""
from copy import deepcopy
import pytest
from test_compatible_epoch import setup
from jev_factorio import compatible_recovery as recovery


def extend(record, target, identifier):
    result = {key: deepcopy(record[key]) for key in recovery._KEYS}
    result.update(previous_source=deepcopy(record["current_source"]),
                  current_source=deepcopy(target), authorization_id=identifier)
    result["authorization_sha256"] = recovery.digest_json(result)
    return result


def test_signed_epoch_followed_by_multiple_forward_equal_edges(tmp_path, monkeypatch):
    memory, old, epoch = setup(tmp_path, monkeypatch)
    memory.compatible_source_recoveries.append(epoch)
    third = extend(epoch, {"commit": "a" * 40, "source_sha256": "b" * 64}, "equal-edge-1")
    fourth = extend(third, {"commit": "c" * 40, "source_sha256": "d" * 64}, "equal-edge-2")
    memory.compatible_source_recoveries.extend([third, fourth])
    before = deepcopy(memory.compatible_source_recoveries)
    assert recovery.validate_lineage(memory) == before
    assert recovery.approved_sources(memory, fourth["current_source"]) == [
        epoch["previous_source"], epoch["current_source"], third["current_source"], fourth["current_source"]]
    assert memory.compatible_source_recoveries == before
    assert old["previous_source"] not in recovery.approved_sources(memory, fourth["current_source"])


@pytest.mark.parametrize("target", ["first_previous", "first_current", "epoch_previous", "self"])
def test_genuine_backward_and_self_edges_rejected(tmp_path, monkeypatch, target):
    memory, old, epoch = setup(tmp_path, monkeypatch)
    memory.compatible_source_recoveries.append(epoch)
    endpoint = {"first_previous": old["previous_source"], "first_current": old["current_source"],
                "epoch_previous": epoch["previous_source"], "self": epoch["current_source"]}[target]
    third = extend(epoch, endpoint, "backward-edge")
    memory.compatible_source_recoveries.append(third)
    with pytest.raises(ValueError, match="cycle|directed chain"):
        recovery.validate_lineage(memory)


def test_forward_record_cannot_hide_invalid_prior_witness(tmp_path, monkeypatch):
    memory, old, epoch = setup(tmp_path, monkeypatch)
    memory.compatible_source_recoveries.append(epoch)
    third = extend(epoch, {"commit": "a" * 40, "source_sha256": "b" * 64}, "equal-edge")
    memory.compatible_source_recoveries.append(third)
    epoch["epoch_witness"]["body"]["prior_lineage_sha256"] = "d" * 64
    with pytest.raises(ValueError): recovery.validate_lineage(memory)
