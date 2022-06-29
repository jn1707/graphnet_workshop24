from abc import ABC, abstractmethod
from collections import OrderedDict
import itertools
from multiprocessing import Pool, Value
import os
import re
import numpy as np
import pandas as pd
from typing import Any, Callable, List, Optional, Tuple, Union
from tqdm import tqdm

try:
    from typing import final
except ImportError:  # Python version < 3.8

    def final(f):  # Identity decorator
        return f


from graphnet.data.utilities.random import pairwise_shuffle
from graphnet.data.extractors import (
    I3Extractor,
    I3ExtractorCollection,
    I3FeatureExtractor,
    I3TruthExtractor,
)
from graphnet.utilities.filesys import find_i3_files
from graphnet.utilities.logging import LoggerMixin, get_logger

logger = get_logger()

try:
    from icecube import icetray, dataio  # pyright: reportMissingImports=false
except ImportError:
    logger.warning("icecube package not available.")


SAVE_STRATEGIES = [
    "1:1",
    "sequential_batched",
    "pattern_batched",
]


class DataConverter(ABC, LoggerMixin):
    """Abstract base class for specialised (SQLite, parquet, etc.) converters."""

    @property
    @abstractmethod
    def file_suffix(self) -> str:
        """Suffix to use on output files."""

    def __init__(
        self,
        extractors: List[I3Extractor],
        outdir: str,
        gcd_rescue: str,
        *,
        nb_files_to_batch: Optional[int] = None,
        sequential_batch_pattern: Optional[str] = None,
        input_file_batch_pattern: Optional[str] = None,
        workers: int = 1,
        index_column: str = "event_no",
        icetray_verbose: int = 0,
    ):
        """Constructor"""

        # Check(s)
        if not isinstance(extractors, (list, tuple)):
            extractors = [extractors]

        assert (
            len(extractors) > 0
        ), "Please specify at least one argument of type I3Extractor"

        for extractor in extractors:
            assert isinstance(
                extractor, I3Extractor
            ), f"{type(extractor)} is not a subclass of I3Extractor"

        assert isinstance(extractors[0], I3TruthExtractor), (
            f"The first extractor in {self.__class__.__name__} should always be of type "
            "I3TruthExtractor to allow for attaching unique indices."
        )

        # Infer saving strategy
        save_strategy = self._infer_save_strategy(
            nb_files_to_batch,
            sequential_batch_pattern,
            input_file_batch_pattern,
        )

        # Member variables
        self._outdir = outdir
        self._gcd_rescue = gcd_rescue
        self._save_strategy = save_strategy
        self._nb_files_to_batch = nb_files_to_batch
        self._sequential_batch_pattern = sequential_batch_pattern
        self._input_file_batch_pattern = input_file_batch_pattern
        self._workers = workers
        self._output_files = []

        # Create I3Extractors
        self._extractors = I3ExtractorCollection(*extractors)

        # Create shorthand of names of all pulsemaps queried
        self._table_names = [extractor.name for extractor in self._extractors]
        self._pulsemaps = [
            extractor.name
            for extractor in self._extractors
            if isinstance(extractor, I3FeatureExtractor)
        ]

        # Shared variable for sequential event indices
        self._index_column = index_column
        self._index = Value("i", 0)

        # Set verbosity
        if icetray_verbose == 0:
            icetray.I3Logger.global_logger = icetray.I3NullLogger()

    @final
    def __call__(self, directories: Union[str, List[str]]):
        """Main call to convert I3 files in `directories.

        Args:
            directories (Union[str, List[str]]): One or more directories, the I3
                files within which should be converted to an intermediate file
                format.
        """
        # Find all I3 and GCD files in the specified directories.
        i3_files, gcd_files = find_i3_files(directories, self._gcd_rescue)
        if len(i3_files) == 0:
            self.logger.info(f"ERROR: No files found in: {directories}.")
            return

        # Save a record of the found I3 files in the output directory.
        self._save_filenames(i3_files)

        # Shuffle I3 files to get a more uniform load on worker nodes.
        i3_files, gcd_files = pairwise_shuffle(i3_files, gcd_files)

        # Implementation-specific initialisation.
        self.initialise()

        # Process the files
        self.execute(i3_files, gcd_files)

        # Implementation-specific finalisation
        self.finalise()

    @final
    def execute(self, i3_files: List[str], gcd_files: List[str]):
        """General method for processing a set of I3 files.

        The files are converted individually according to the inheriting class/
        intermediate file format.

        Args:
            i3_files (List[str]): List of paths to I3 files.
            gcd_files (List[str]): List of paths to corresponding GCD files.
        """

        # Make sure output directory exists.
        self.logger.info(f"Saving results to {self._outdir}")
        os.makedirs(self._outdir, exist_ok=True)

        # Construct the list of arguments to be passed to `_process_file`.
        args = list(zip(i3_files, gcd_files))

        # Iterate over batches of files.
        try:
            if self._save_strategy == "sequential_batched":
                batches = np.array_split(
                    args,
                    int(np.ceil(len(args) / self._nb_files_to_batch)),
                )
                self._iterate_over_and_sequentially_batch_individual_files(
                    args
                )

            elif self._save_strategy == "pattern_batched":
                groups = OrderedDict()
                for i3_file, gcd_file in sorted(args, key=lambda p: p[0]):
                    group = re.sub(
                        self._sub_from, self._sub_to, os.path.basename(i3_file)
                    )
                    if group not in groups:
                        groups[group] = list()
                    groups[group].append((i3_file, gcd_file))

                self.logger.info(
                    f"Will batch {len(i3_files)} input files into {len(groups)} groups"
                )
                if len(groups) <= 20:
                    for group, files in groups.items():
                        self.logger.info(f"> {group}: {len(files):3d} file(s)")

                batches = [
                    (list(group_args), group)
                    for group, group_args in groups.items()
                ]
                self._iterate_over_batches_of_files(batches)

            elif self._save_strategy == "1:1":
                self._iterate_over_individual_files(args)

            else:
                assert False, "Shouldn't reach here."

        except KeyboardInterrupt:
            self.logger.warning("[ctrl+c] Exciting gracefully.")

    @abstractmethod
    def save_data(self, data: List[OrderedDict], output_file: str):
        """Implementation-specific method for saving data to file.

        Args:
            data (List[OrderedDict]): List of extracted features.
            output_file (str): Name of output file.
        """

    @abstractmethod
    def merge_files(
        self, output_file: str, input_files: Optional[List[str]] = None
    ):
        """Implementation-specific method for merging output files.

        Args:
            output_file (str): Name of the output file containing the merged
                results.
            input_files (List[str]): Intermediate files to be merged, according
                to the specific implementation. Default to None, meaning that
                all files output by the current instance are merged.
        """

    def initialise(self):
        """Implementation-specific initialisation before each call."""

    def finalise(self):
        """Implementation-specific finalisation after each call."""

    # Internal methods
    def _iterate_over_individual_files(self, args: List[Tuple[str, str]]):
        # Get appropriate mapping function
        map_fn = self.get_map_function(len(args))

        # Iterate over files
        for _ in map_fn(
            self._process_file, tqdm(args, unit="file(s)", colour="green")
        ):
            self.logger.debug(
                "Saving with 1:1 strategy on the individual worker processes"
            )

    def _iterate_over_and_sequentially_batch_individual_files(
        self, args: List[Tuple[str, str]]
    ):
        # Get appropriate mapping function
        map_fn = self.get_map_function(len(args))

        # Iterate over files
        dataset = list()
        ix_batch = 0
        for ix, data in enumerate(
            map_fn(
                self._process_file,
                tqdm(args, unit="file(s)", colour="green"),
            )
        ):
            dataset.extend(data)
            if (ix + 1) % self._nb_files_to_batch == 0:
                self.logger.debug(
                    "Saving with batched strategy on the main processs"
                )
                self.save_data(
                    dataset,
                    self._get_output_file(
                        self._sequential_batch_pattern.format(ix_batch)
                    ),
                )
                ix_batch += 1
                del dataset
                dataset = list()

        if len(dataset) > 0:
            self.save_data(
                dataset,
                self._get_output_file(
                    self._sequential_batch_pattern.format(ix_batch)
                ),
            )

    def _iterate_over_batches_of_files(
        self, args: List[Tuple[List[Tuple[str, str]], str]]
    ):
        # Get appropriate mapping function
        map_fn = self.get_map_function(len(args), unit="batch(es)")

        # Iterate over batches of files
        for _ in map_fn(
            self._process_batch, tqdm(args, unit="batch(es)", colour="green")
        ):
            self.logger.debug("Saving with batched strategy")

    def _process_batch(
        self, args: Tuple[List[Tuple[str, str]], str]
    ) -> Optional[List[OrderedDict]]:
        # Unpack arguments
        batch_args, output_file_name = args

        # Process individual files
        data = list(
            itertools.chain.from_iterable(map(self._process_file, batch_args))
        )

        # (Opt.) Save batched data
        self.save_data(data, self._get_output_file(output_file_name))

        return data

    def _process_file(
        self, args: Tuple[str, str]
    ) -> Optional[List[OrderedDict]]:
        """Implementation-specific method for converting single I3 file.

        Also works recursively on a list of I3 files.

        Args:
            i3_file (str): Path to I3 file.
            gcd_file (str): Path to corresponding GCD file.
        """

        # Unpack arguments
        i3_file, gcd_file = args

        self._extractors.set_files(i3_file, gcd_file)
        i3_file_io = dataio.I3File(i3_file, "r")
        data = list()
        while i3_file_io.more():
            try:
                frame = i3_file_io.pop_physics()
            except:  # noqa: E722
                continue

            # Extract data from I3Frame
            results = self._extractors(frame)
            data_dict = OrderedDict(zip(self._table_names, results))

            # Get new, unique index and increment value
            with self._index.get_lock():
                index = self._index.value
                self._index.value += 1

            # Attach index to all tables
            for table in data_dict.keys():
                data_dict[table][self._index_column] = index

            data.append(data_dict)

        if self._save_strategy == "1:1":
            self.save_data(data, self._get_output_file(i3_file))
            return

        return data

    def get_map_function(
        self, nb_files: int, unit: str = "I3 file(s)"
    ) -> Callable:
        """Identify the type of map function to use (pure python or multiprocess)."""

        # Choose relevant map-function given the requested number of workers.
        workers = min(self._workers, nb_files)
        if workers > 1:
            self.logger.info(
                f"Starting pool of {workers} workers to process {nb_files} {unit}"
            )
            p = Pool(processes=workers)
            map_fn = p.imap

        else:
            self.logger.info(
                f"Processing {nb_files} {unit} in main thread (not multiprocessing)"
            )
            map_fn = map

        return map_fn

    def _infer_save_strategy(
        self,
        nb_files_to_batch: Optional[int] = None,
        sequential_batch_pattern: Optional[str] = None,
        input_file_batch_pattern: Optional[str] = None,
    ) -> str:
        if input_file_batch_pattern is not None:
            save_strategy = "pattern_batched"

            assert (
                "*" in input_file_batch_pattern
            ), "Argument `input_file_batch_pattern` should contain at least one wildcard (*)"

            fields = [
                "(" + field + ")"
                for field in input_file_batch_pattern.replace(
                    ".", r"\."
                ).split("*")
            ]
            nb_fields = len(fields)
            self._sub_from = ".*".join(fields)
            self._sub_to = "x".join([f"\\{ix + 1}" for ix in range(nb_fields)])

            if sequential_batch_pattern is not None:
                self.logger.warning(
                    "Argument `sequential_batch_pattern` ignored."
                )
            if nb_files_to_batch is not None:
                self.logger.warning("Argument `nb_files_to_batch` ignored.")

        elif (nb_files_to_batch is not None) or (
            sequential_batch_pattern is not None
        ):
            save_strategy = "sequential_batched"

            assert (nb_files_to_batch is not None) and (
                sequential_batch_pattern is not None
            ), "Please specify both `nb_files_to_batch` and `sequential_batch_pattern` for sequential batching."

        else:
            save_strategy = "1:1"

        return save_strategy

    def _save_filenames(self, i3_files: List[str]):
        """Saves I3 file names in CSV format."""
        self.logger.debug("Saving input file names to config CSV.")
        config_dir = os.path.join(self._outdir, "config")
        os.makedirs(config_dir, exist_ok=True)
        i3_files = pd.DataFrame(data=i3_files, columns=["filename"])
        i3_files.to_csv(os.path.join(config_dir, "i3files.csv"))

    def _get_output_file(self, input_file: str) -> str:
        assert isinstance(input_file, str)
        basename = os.path.basename(input_file)
        output_file = os.path.join(
            self._outdir,
            re.sub(r"\.i3\..*", "", basename) + "." + self.file_suffix,
        )
        return output_file
