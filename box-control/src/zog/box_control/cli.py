from .api import BoxControl


def main() -> int:
    report = BoxControl.discover().evaluate()
    print(report.summary())
    return 0 if report.ok else 1
