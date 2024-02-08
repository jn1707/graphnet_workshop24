"""Example of comparing the result of converting to SQLite and Parquet."""

import os

from graphnet.constants import EXAMPLE_OUTPUT_DIR, TEST_DATA_DIR
from graphnet.data.constants import FEATURES, TRUTH
from graphnet.data.sqlite import SQLiteDataConverter
from graphnet.data.parquet import ParquetDataConverter
from graphnet.data.dataset import SQLiteDataset, ParquetDataset
from graphnet.data.extractors.icecube import (
    I3FeatureExtractorIceCube86,
    I3TruthExtractor,
    I3RetroExtractor,
)
from graphnet.utilities.argparse import ArgumentParser
from graphnet.utilities.imports import has_icecube_package
from graphnet.utilities.logging import Logger

ERROR_MESSAGE_MISSING_ICETRAY = (
    "This example requires IceTray to be installed, which doesn't seem to be "
    "the case. Please install IceTray; run this example in the GraphNeT "
    "Docker container which comes with IceTray installed; or run an example "
    "script in one of the other folders:"
    "\n * examples/02_data/"
    "\n * examples/03_weights/"
    "\n * examples/04_training/"
    "\n * examples/05_pisa/"
    "\nExiting."
)

OUTPUT_DIR = f"{EXAMPLE_OUTPUT_DIR}/compare_sqlite_and_parquet"
PULSEMAP = "SRTInIcePulses"


def convert_data() -> None:
    """Convert I3 files to SQLite and Parquet."""
    # Configuration
    path = f"{TEST_DATA_DIR}/i3/oscNext_genie_level7_v02/"

    opt = dict(
        extractors=[
            I3TruthExtractor(),
            I3RetroExtractor(),
            I3FeatureExtractorIceCube86(PULSEMAP),
        ],
        outdir=OUTPUT_DIR,
        workers=1,
    )

    # Run data converters.
    SQLiteDataConverter(**opt)(path)  # type: ignore[arg-type]
    ParquetDataConverter(**opt)(path)  # type: ignore[arg-type]


def load_data() -> None:
    """Load converted data and compare.."""
    filename = "oscNext_genie_level7_v02_first_5_frames"

    opt = dict(
        pulsemaps=PULSEMAP,
        features=FEATURES.DEEPCORE,
        truth=TRUTH.DEEPCORE,
    )

    data_sqlite = SQLiteDataset(
        os.path.join(OUTPUT_DIR, filename + ".db"),
        **opt,  # type: ignore[arg-type]
    )
    data_parquet = ParquetDataset(
        os.path.join(OUTPUT_DIR, filename + ".parquet"),
        **opt,  # type: ignore[arg-type]
    )

    logger = Logger()
    logger.info(f"Number of events in SQLiteDataset: {len(data_sqlite)}")
    logger.info(f"Number of events in Parquetataset: {len(data_parquet)}")

    print(data_sqlite[0])
    print(data_parquet[0])

    print(data_sqlite[0].x == data_parquet[0].x)


if __name__ == "__main__":

    if not has_icecube_package():
        Logger(log_folder=None).error(ERROR_MESSAGE_MISSING_ICETRAY)

    else:
        # Parse command-line arguments
        parser = ArgumentParser(
            description="""
Convert I3 files to both SQLite and Parquet formats, and see that the results
agree.
"""
        )

        args, unknown = parser.parse_known_args()

        # Run example script(s)
        convert_data()
        load_data()
