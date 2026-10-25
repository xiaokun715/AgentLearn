"""Word Count job: business logic separated from execution engine."""
import re
from pathlib import Path

from minimapreduce.engine import MiniMapReduce


def mapper(line: str):
    """Emit (word, 1) for each word in one input record."""
    for word in re.findall(r"[A-Za-z0-9']+", line.lower()):
        yield word, 1


def reducer(word: str, counts: list[int]) -> int:
    """Sum all values associated with a word."""
    return sum(counts)


def main():
    project_root = Path(__file__).resolve().parents[1]
    engine = MiniMapReduce(reducers=3)
    engine.run_file(
        input_path=project_root / "data" / "words.txt",
        output_path=project_root / "output" / "word_count.json",
        mapper=mapper,
        reducer=reducer,
        mapper_count=3,
    )


if __name__ == "__main__":
    main()
