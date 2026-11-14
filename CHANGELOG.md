# Changelog

## November 2026

- OpenShift deployment validation: reject fresh OCP installs before placement or job submission when the project owner has no pull secret; direct users to configure an OCP pull secret or pull-through registry credentials, while allowing OKD and captured-cluster re-certification. Bulk Deploy reports failures by project while continuing other deployments. Confirmed installer failures show “OpenShift Install Failed” with a safe reason, and API timeouts explicitly identify OpenShift without replacing a confirmed install failure.
