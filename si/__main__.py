"""Allow `python -m si`."""

try:
    from .cli import main
except ImportError as e:  # missing HTTP backend from compat, or a bad install
    print(f"[!] {e}")
    raise SystemExit(1) from None

if __name__ == "__main__":
    main()
