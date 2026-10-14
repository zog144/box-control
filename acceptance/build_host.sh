#!/bin/bash
set -euo pipefail
mkdir -p evidence
exec > >(tee evidence/host.log) 2>&1
dnf install -y python3.11 python3.11-pip dbus-daemon
python3.11 -m venv /root/zog-build-test-environment
/root/zog-build-test-environment/bin/pip install -e './box-control[test]' -e ../dependencies/zog144/image-build -e ../dependencies/zog144/root-control
id zogbuildtest >/dev/null 2>&1 || useradd -u 21001 -M -s /sbin/nologin zogbuildtest
/root/zog-build-test-environment/bin/python acceptance/build_live.py
