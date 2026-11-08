"""Entry point."""

from cli import build_parser
from config import RuntimeConfig


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = RuntimeConfig(args.name)
    return cfg.message()


if __name__ == "__main__":
    print(main())
