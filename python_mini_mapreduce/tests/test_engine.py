import unittest

from minimapreduce.engine import MiniMapReduce


def mapper(line):
    for token in line.split():
        yield token.lower(), 1


def reducer(key, values):
    return sum(values)


class TestMiniMapReduce(unittest.TestCase):
    def test_word_count(self):
        engine = MiniMapReduce(reducers=3)
        result = engine.run(
            ["cat dog cat", "dog bird"],
            mapper=mapper,
            reducer=reducer,
            mapper_count=2,
        )
        self.assertEqual(result, {"cat": 2, "dog": 2, "bird": 1})

    def test_empty_input(self):
        engine = MiniMapReduce()
        result = engine.run([], mapper, reducer)
        self.assertEqual(result, {})

    def test_invalid_reducer_count(self):
        with self.assertRaises(ValueError):
            MiniMapReduce(reducers=0)


if __name__ == "__main__":
    unittest.main()
