"""Stale reconfigure progress must not blank VM status on active projects."""

from app.services.ws_pubsub import _should_skip_vm_poll_for_progress


def test_no_progress_does_not_skip():
    assert _should_skip_vm_poll_for_progress(None) is False
    assert _should_skip_vm_poll_for_progress({}) is False


def test_ocp_install_progress_keeps_polling():
    assert (
        _should_skip_vm_poll_for_progress({"step": "ocp-install", "detail": "recert"})
        is False
    )
    assert _should_skip_vm_poll_for_progress({"step": "control-plane-usable"}) is False


def test_stale_reconfigure_progress_does_not_skip():
    """Apply Changes uses state=reconfiguring (not in this poller). Leftover
    deploy:{id}={reconfigure, waiting for VMs} on an active project must not
    suppress KubeVirt state — that left cards spinning forever."""
    assert (
        _should_skip_vm_poll_for_progress(
            {"step": "reconfigure", "detail": "waiting for VMs"}
        )
        is False
    )
    assert (
        _should_skip_vm_poll_for_progress(
            {"step": "reconfiguring", "detail": "source-worker-0"}
        )
        is False
    )


def test_early_deploy_progress_still_skips():
    assert (
        _should_skip_vm_poll_for_progress({"step": "creating", "detail": "VMs"}) is True
    )
    assert (
        _should_skip_vm_poll_for_progress({"step": "downloading", "detail": "0%"})
        is True
    )
