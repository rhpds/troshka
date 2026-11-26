"""Small reviewed knowledge set shipped with the running release."""

from app.services.assistant.contracts import Evidence

ARTICLES = (
    (
        "ssh-forwarding",
        "ssh port nat timeout connection",
        "Use lowercase ssh -p for the configured external port. Compare the forward's target with the VM's observed address and running state. A web route on 80/443 does not provide SSH. Verify from the user's source network; escalate if the configured path and runtime disagree.",
    ),
    (
        "dhcp-collision",
        "dhcp ip address lease collision",
        "Compare the configured reservation, current guest address and observed MAC before changing a lease. An ops/gateway address collision can prevent the reserved address being assigned. Escalate shared-network conflicts; do not wipe leases or recreate pods from chat.",
    ),
    (
        "bmc-routing",
        "bmc redfish bastion network",
        "BMC management addresses are separate from guest addresses. A successful ops-pod probe does not prove the bastion has a route. Compare guest interfaces and routes with the BMC subnet; never assign the guest an address already used by the BMC endpoint.",
    ),
    (
        "ocp-credentials",
        "ocp install pullsecret api unreachable",
        "ACTIVE describes infrastructure, not successful OpenShift installation. Inspect the earliest installer failure. A missing pull secret can prevent ISO generation, followed by an API timeout. Configure credentials in Settings, then use the existing Retry Install control and verify installer completion and API readiness.",
    ),
    (
        "nic-network",
        "nic networkref disconnected deploy timeout",
        "Check that every required NIC connects to the intended network. Correct the canvas configuration through the existing editor. A terminal missing-networkRef condition is not solved by waiting longer. Verify the VM becomes Ready after an explicitly requested deployment.",
    ),
    (
        "cleanup-phases",
        "delete cleanup pending finalizer ownership",
        "Inspect the cleanup phase and remaining blocker. A namespace finalizer, missing ownership evidence and incomplete storage measurement are different failures. Do not remove finalizers or bypass ownership. Escalate with phase and evidence; use existing retry controls only after the blocker is corrected.",
    ),
    (
        "storage-charges",
        "stop disk storage cost charge billing",
        "Stopping VMs preserves disks. Namespace absence alone does not prove all retained storage has been reclaimed. Report compute state and storage settlement separately. Do not promise that charges stopped unless accepted accounting evidence confirms it.",
    ),
)


def search_help(query, release="bundled", provider="any"):
    words = set(query.lower().split())
    ranked = sorted(
        ARTICLES, key=lambda a: len(words & set(a[1].split())), reverse=True
    )
    return [
        {
            "id": a[0],
            "release": "bundled with running application",
            "provider": "any",
            "guidance": a[2],
        }
        for a in ranked
        if words & set(a[1].split())
    ][:3]


def help_check(db, ctx, args):
    return Evidence(
        check="search_help",
        project_id=ctx.project_id,
        source="reviewed bundled guidance",
        facts={"articles": search_help(args.query)},
    )
