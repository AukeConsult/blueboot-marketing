"""batch_test.py — Smoke test script for the cloud_batch runner.

Prints environment info and exits cleanly. Used by test_job to verify
the Cloud Run service can execute pipeline steps correctly.

Usage:
    python -m app.batch_test --step 1 --message "Hello"
"""
import argparse
import os
import platform
import sys
import time


def main(argv=None):
    parser = argparse.ArgumentParser(description="Batch runner smoke test")
    parser.add_argument("--step",    default="1",     help="Step number (for display)")
    parser.add_argument("--message", default="OK",    help="Message to print")
    args = parser.parse_args(argv)

    print(f"=== Batch Test — Step {args.step} ===")
    print(f"Message:  {args.message}")
    print(f"Python:   {sys.version.split()[0]}")
    print(f"Platform: {platform.system()} {platform.release()}")
    print(f"CWD:      {os.getcwd()}")
    print(f"GCP project: {os.getenv('GCP_PROJECT', '(not set)')}")

    # Simulate a tiny bit of work
    for i in range(1, 4):
        print(f"  working... {i}/3")
        time.sleep(1)

    print(f"Step {args.step} complete.")
    sys.exit(0)


if __name__ == "__main__":
    main()
