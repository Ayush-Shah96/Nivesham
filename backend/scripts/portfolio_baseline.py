from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow direct execution: `python scripts/portfolio_baseline.py ...`
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from apps.portfolio_comparison.service import create_upload_batch, process_batch
from ii_platform.database.connection import get_sessionmaker


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed one historical Portfolio Manager month from a ZIP archive."
    )
    parser.add_argument("--month", required=True, help="Historical month in YYYY-MM format, e.g. 2026-08")
    parser.add_argument("--zip", required=True, dest="zip_path", help="Path to the Portfolio Appraisal ZIP")
    return parser.parse_args()


async def main_async() -> None:
    args = parse_args()
    zip_path = Path(args.zip_path).expanduser().resolve()
    if not zip_path.exists():
        raise FileNotFoundError(f"ZIP file not found: {zip_path}")
    if zip_path.suffix.lower() != ".zip":
        raise ValueError("--zip must point to a .zip archive")

    archive_bytes = zip_path.read_bytes()
    async with get_sessionmaker()() as db:
        batch_id = await create_upload_batch(
            db,
            user_id=None,
            comparison_month=args.month,
            archive_filename=zip_path.name,
            archive_bytes=archive_bytes,
            upload_type="baseline",
        )

    print(f"Baseline batch queued: {batch_id}")
    result = await process_batch(batch_id)
    print("Baseline import result:")
    print(result)


if __name__ == "__main__":
    asyncio.run(main_async())
