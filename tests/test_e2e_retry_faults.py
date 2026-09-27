"""再試行E2Eの待機・隔離・再投入と別読戻しの契約。"""

import json

import pytest

from tests.test_e2e_sync_fault_probe import env, manifest, prepare_all, request, run
from e2e_sync_fault_probe import FaultKV
from state import StateStore

__all__ = ["env"]


@pytest.mark.parametrize("case", ["retry_wait", "retry_quarantine"])
def test_retry_policy_evidence_and_cleanup(env, case):
    original = dict(env.STATE_KV.data)
    assert run(prepare_all(env))[0] == 200
    owner = manifest(env)
    assert case in owner["hashes"]
    evidence = json.loads(env.STATE_KV.data[FaultKV(StateStore(env), owner, case).prefix + "evidence"])
    assert evidence["later_event_processed"] is True
    assert evidence["notification_posts"] == 2
    if case == "retry_wait":
        assert evidence["retry_seconds"] == 300
        assert evidence["attempts"] == 1
    else:
        assert evidence["attempts"] == 6
        assert evidence["quarantine_skipped"] is True
        assert evidence["requeue_idempotent"] is True
        assert evidence["message_id_preserved"] is True
        assert evidence["queue_drained"] is True
    assert run(request(env, "/verify"))[0] == 200
    assert run(request(env, "/cleanup"))[0] == 200
    assert env.STATE_KV.data == original
