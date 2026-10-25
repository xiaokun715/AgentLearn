"""A tiny, single-process MapReduce teaching engine."""
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, TypeVar

K = TypeVar("K")
V = TypeVar("V")
R = TypeVar("R")

MapOutput = Iterable[tuple[K, V]]
Mapper = Callable[[str], MapOutput]
Reducer = Callable[[K, list[V]], R]


@dataclass
class RunStats:
    input_records: int = 0
    map_output_records: int = 0
    reducer_count: int = 0
    output_keys: int = 0


class MiniMapReduce:
    """Simulates split -> map -> partition -> shuffle/sort -> reduce."""

    def __init__(self, reducers: int = 2):
        if reducers < 1:
            raise ValueError("reducers must be >= 1")
        self.reducers = reducers
        self.stats = RunStats(reducer_count=reducers)

    @staticmethod
    def split_input(lines: list[str], splits: int) -> list[list[str]]:
        """Split records into at most `splits` balanced chunks."""
        if splits < 1:
            raise ValueError("splits must be >= 1")
        if not lines:
            return []
        chunks = min(splits, len(lines))
        quotient, remainder = divmod(len(lines), chunks)
        result, start = [], 0
        for index in range(chunks):
            size = quotient + (1 if index < remainder else 0)
            result.append(lines[start:start + size])
            start += size
        return result

    @staticmethod
    def partition(key: object, reducers: int) -> int:
        """Choose the reducer for a key.

        Python's built-in hash may vary between processes; that is fine for this
        local demo because we do not persist/reuse partition assignments.
        """
        return hash(key) % reducers

    def run(
        self,
        lines: list[str],
        mapper: Mapper,
        reducer: Reducer,
        mapper_count: int = 3,
    ) -> dict[K, R]:
        if mapper_count < 1:
            raise ValueError("mapper_count must be >= 1")

        self.stats = RunStats(reducer_count=self.reducers)
        self.stats.input_records = len(lines)

        splits = self.split_input(lines, mapper_count)
        reducer_buckets: list[list[tuple[K, V]]] = [
            [] for _ in range(self.reducers)
        ]

        # Map: each split is processed by one logical Mapper.
        for mapper_id, split in enumerate(splits):
            print(f"[MAP] mapper={mapper_id}, records={len(split)}")
            for record in split:
                for key, value in mapper(record):
                    reducer_id = self.partition(key, self.reducers)
                    reducer_buckets[reducer_id].append((key, value))
                    self.stats.map_output_records += 1

        # Shuffle/Sort: within each reducer, group equal keys together.
        results: dict[K, R] = {}
        for reducer_id, pairs in enumerate(reducer_buckets):
            grouped: dict[K, list[V]] = defaultdict(list)
            for key, value in pairs:
                grouped[key].append(value)

            print(
                f"[SHUFFLE] reducer={reducer_id}, "
                f"intermediate_pairs={len(pairs)}, keys={len(grouped)}"
            )

            # Sorting keys is for deterministic teaching output; Hadoop's exact
            # sort behavior depends on key comparators and partitioning.
            for key in sorted(grouped, key=lambda item: str(item)):
                results[key] = reducer(key, grouped[key])

        self.stats.output_keys = len(results)
        return results

    def run_file(
        self,
        input_path: str | Path,
        output_path: str | Path,
        mapper: Mapper,
        reducer: Reducer,
        mapper_count: int = 3,
    ) -> dict:
        input_path = Path(input_path)
        output_path = Path(output_path)
        lines = input_path.read_text(encoding="utf-8").splitlines()
        results = self.run(lines, mapper, reducer, mapper_count)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        import json
        output_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(
            f"[DONE] input={self.stats.input_records}, "
            f"mapped={self.stats.map_output_records}, "
            f"output_keys={self.stats.output_keys}, file={output_path}"
        )
        return results
