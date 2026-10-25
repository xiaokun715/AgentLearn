import argparse
import re
from pathlib import Path

from minimapreduce.engine import MiniMapReduce


def mapper(line: str):
    for word in re.findall(r"[A-Za-z0-9']+", line.lower()):
        yield word, 1


def reducer(word: str, counts: list[int]) -> int:
    return sum(counts)


def main():
    parser = argparse.ArgumentParser(description="Python Mini MapReduce")
    parser.add_argument("--input", default="data/words.txt")
    parser.add_argument("--output", default="output/result.json")
    parser.add_argument("--reducers", type=int, default=3)
    parser.add_argument("--mappers", type=int, default=3)
    args = parser.parse_args()

    engine = MiniMapReduce(reducers=args.reducers)
    engine.run_file(args.input, args.output, mapper, reducer, args.mappers)


if __name__ == "__main__":
    main()
