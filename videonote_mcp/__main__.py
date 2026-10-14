"""Dispatch installation before importing the CLI's database/runtime setup."""
import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "install":
        from videonote_mcp.installer import main as install

        raise SystemExit(install(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "_install-runtime":
        from videonote_mcp.installer_runtime import main as runtime

        raise SystemExit(runtime(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print("VideoNote: install | setup | transcriber | login | providers | proxy | cookie | export")
        print("Run without arguments to serve MCP over stdio.")
        return
    from videonote_mcp.cli import main as cli

    cli()


if __name__ == "__main__":
    main()
