application(
    name="hello",
    programs=(
        program(
            name="main",
            command=("/bin/sh", "-c", "echo 'box-control hello'; sleep 2"),
        ),
    ),
    start_policy="start-once-per-boot",
)
