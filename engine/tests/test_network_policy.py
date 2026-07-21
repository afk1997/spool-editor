from __future__ import annotations

import threading

import pytest

from network_policy import NetworkPolicy, NetworkPolicyError


def test_egress_lease_counts_and_releases_on_success_and_error():
    policy = NetworkPolicy()

    assert policy.offline is False
    assert policy.active_leases == 0
    with policy.egress("url_download"):
        assert policy.active_leases == 1
        with policy.egress("model_download"):
            assert policy.active_leases == 2
        assert policy.active_leases == 1
    assert policy.active_leases == 0

    with pytest.raises(RuntimeError, match="socket failed"):
        with policy.egress("watch_listing"):
            assert policy.active_leases == 1
            raise RuntimeError("socket failed")
    assert policy.active_leases == 0


def test_offline_rejects_a_new_lease_without_incrementing():
    policy = NetworkPolicy()
    policy.enable_offline()

    with pytest.raises(NetworkPolicyError) as denied:
        with policy.egress("model_download"):
            raise AssertionError("offline lease body must not run")

    assert denied.value.code == "offline_network_disabled"
    assert denied.value.error_category == "offline_network_disabled"
    assert denied.value.purpose == "model_download"
    assert policy.offline is True
    assert policy.active_leases == 0


def test_network_policy_error_category_matches_code():
    error = NetworkPolicyError("network_work_active")

    assert error.error_category == error.code == "network_work_active"


def test_active_lease_rejects_offline_transition_without_changing_state():
    policy = NetworkPolicy()

    with policy.egress("codex_reasoning"):
        with pytest.raises(NetworkPolicyError) as denied:
            policy.enable_offline()
        assert denied.value.code == "network_work_active"
        assert policy.offline is False
        assert policy.active_leases == 1

    assert policy.active_leases == 0
    policy.enable_offline()
    assert policy.offline is True


def test_failed_atomic_transition_rolls_back_policy_state():
    policy = NetworkPolicy()

    with pytest.raises(OSError, match="disk full"):
        with policy.transition(True):
            raise OSError("disk full")

    assert policy.offline is False
    with policy.egress("url_validation"):
        assert policy.active_leases == 1


@pytest.mark.parametrize("initial_offline", [False, True])
def test_transition_none_holds_the_lock_without_changing_offline_state(initial_offline):
    policy = NetworkPolicy(offline=initial_offline)

    with policy.transition(None):
        assert policy.offline is initial_offline

    assert policy.offline is initial_offline


def test_enabling_transition_rejects_reentrant_egress_before_commit():
    policy = NetworkPolicy()

    with policy.transition(True):
        assert policy.offline is True
        with pytest.raises(NetworkPolicyError) as denied:
            with policy.egress("reentrant_download"):
                raise AssertionError("transition must block reentrant egress")
        assert denied.value.code == "offline_network_disabled"

    assert policy.offline is True
    assert policy.active_leases == 0


def test_transition_linearizes_against_new_egress_leases():
    policy = NetworkPolicy()
    transition_entered = threading.Event()
    release_transition = threading.Event()
    transition_done = threading.Event()
    lease_attempted = threading.Event()
    lease_done = threading.Event()
    result: list[str] = []

    def switch_offline():
        with policy.transition(True):
            transition_entered.set()
            assert release_transition.wait(1)
        transition_done.set()

    def start_lease():
        lease_attempted.set()
        try:
            with policy.egress("url_download"):
                result.append("allowed")
        except NetworkPolicyError as error:
            result.append(error.code)
        finally:
            lease_done.set()

    transition_thread = threading.Thread(target=switch_offline)
    transition_thread.start()
    assert transition_entered.wait(1)

    lease_thread = threading.Thread(target=start_lease)
    lease_thread.start()
    assert lease_attempted.wait(1)
    assert lease_done.wait(0.05) is False

    release_transition.set()
    assert transition_done.wait(1)
    assert lease_done.wait(1)
    transition_thread.join(timeout=1)
    lease_thread.join(timeout=1)

    assert result == ["offline_network_disabled"]
    assert policy.offline is True
    assert policy.active_leases == 0


def test_disabling_offline_reopens_egress():
    policy = NetworkPolicy()
    policy.enable_offline()
    policy.disable_offline()

    with policy.egress("watch_listing"):
        assert policy.offline is False
        assert policy.active_leases == 1


def test_launch_admission_holds_the_transition_lock_inside_an_active_lease():
    policy = NetworkPolicy()
    transition_started = threading.Event()
    transition_done = threading.Event()

    def transition_settings():
        transition_started.set()
        with policy.transition(None):
            pass
        transition_done.set()

    with policy.egress("codex_reasoning") as lease:
        with lease.launch_admission():
            worker = threading.Thread(target=transition_settings)
            worker.start()
            assert transition_started.wait(1)
            assert transition_done.wait(0.05) is False
        assert transition_done.wait(1)

    worker.join(timeout=1)
    assert not worker.is_alive()
    assert policy.active_leases == 0


def test_launch_admission_rejects_a_lease_borrowed_by_another_thread():
    policy = NetworkPolicy()
    lease_ready = threading.Event()
    release_lease = threading.Event()
    leases = []

    def own_lease():
        with policy.egress("codex_reasoning") as lease:
            leases.append(lease)
            lease_ready.set()
            assert release_lease.wait(1)

    owner = threading.Thread(target=own_lease)
    owner.start()
    assert lease_ready.wait(1)

    try:
        with pytest.raises(RuntimeError, match="owning thread"):
            with leases[0].launch_admission():
                raise AssertionError("a foreign thread cannot borrow launch admission")
    finally:
        release_lease.set()
        owner.join(timeout=1)

    assert not owner.is_alive()
    assert policy.active_leases == 0


def test_launch_admission_rejects_an_inactive_lease_token():
    policy = NetworkPolicy()

    with policy.egress("codex_reasoning") as lease:
        pass

    with pytest.raises(RuntimeError, match="active egress lease"):
        with lease.launch_admission():
            raise AssertionError("a released lease cannot launch")
