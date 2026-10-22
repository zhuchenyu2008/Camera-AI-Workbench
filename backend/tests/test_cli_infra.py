from app.cli import _invoke_runner, build_parser


def test_cli_exposes_required_service_modes_and_backup_commands():
    parser = build_parser()
    for command in ("serve", "worker", "scheduler", "capture", "backup", "verify-backup"):
        assert any(action.dest == "command" for action in parser._actions)
        args = [command]
        if command == "backup":
            args += ["--output", "backup.zip"]
        elif command == "verify-backup":
            args += ["backup.zip"]
        parsed = parser.parse_args(args)
        assert parsed.command == command


def test_runner_dispatch_supports_generic_main_argv_and_service_runner():
    captured = []

    def main(argv=None):
        captured.extend(argv)

    def run_worker():
        captured.append("worker-called")

    _invoke_runner(main, "scheduler", ["--once"])
    _invoke_runner(run_worker, "worker", [])
    assert captured == ["scheduler", "--once", "worker-called"]
