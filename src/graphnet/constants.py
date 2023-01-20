"""Global constants for use across `graphnet`."""

import os.path

GRAPHNET_ROOT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)

# Test data
TEST_DATA_DIR = os.path.join(GRAPHNET_ROOT_DIR, "test_data")

_test_dataset_name = "oscNext_genie_level7_v02"
_test_dataset_file = f"{_test_dataset_name}_first_5_frames"
TEST_SQLITE_DATA = os.path.join(
    TEST_DATA_DIR, "sqlite", _test_dataset_name, f"{_test_dataset_file}.db"
)
TEST_PARQUET_DATA = os.path.join(
    TEST_DATA_DIR,
    "parquet",
    _test_dataset_name,
    f"{_test_dataset_file}.parquet",
)
