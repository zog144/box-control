def pytest_addoption(parser):
    parser.addoption("--run-systemd-integration", action="store_true", default=False,
                     help="Run privileged lifecycle tests on a disposable systemd host")
    parser.addoption("--systemd-rootfs", help="Minimal test rootfs containing /bin/sh, /bin/sleep, /bin/true")
