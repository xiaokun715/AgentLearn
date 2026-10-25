# Python Mini MapReduce

一个用于第一天学习 Hadoop / MapReduce 核心流程的单机教学项目。

## 学习目标

理解以下阶段如何衔接：

1. **Input Split**：把输入文件拆成若干输入分片。
2. **Map**：把每条输入记录映射成 `(key, value)`。
3. **Partition**：根据 key 决定中间数据归属哪个 Reducer。
4. **Shuffle / Sort**：按 key 汇集来自不同 Mapper 的中间结果。
5. **Reduce**：对同一个 key 的 values 做聚合。
6. **Output**：将最终结果写入文件。

> 这个项目是单进程教学版，不是真正的分布式 Hadoop。它会模拟多个 Mapper / Reducer 的逻辑分区，但暂时不包含网络通信、进程间调度、节点故障恢复等机制。

## 环境

Python 3.10+，无需第三方依赖。

## 运行

在项目根目录执行：

```bash
python main.py
```

指定输入、输出和 Reducer 数量：

```bash
python main.py --input data/words.txt --output output/result.json --reducers 3
```

运行测试：

```bash
python -m unittest discover -s tests -v
```

## 建议学习顺序

1. 先读 `minimapreduce/engine.py` 的 `run()`，抓住完整数据流。
2. 看 `examples/word_count.py`：业务逻辑只负责定义 `mapper` 和 `reducer`。
3. 在 `engine.py` 中跟踪 `split_input`、`map`、`partition`、`shuffle_sort`、`reduce`。
4. 修改输入文本，预测输出，再运行验证。
5. 试着在 `mapper` 中把单词统一转小写；观察结果如何变化。
6. 把 `reducers=1` 改为 `reducers=3`。最终结果应一致，只是中间分区不同。

## 核心数据流

```text
input file
   |
   v
Input Split
   |
   v
Mapper 0   Mapper 1   Mapper 2
   |          |          |
   +----------+----------+
              |
              v
      intermediate pairs
       (word, 1), ...
              |
              v
        Partition by key
        /       |       \
   Reducer 0 Reducer 1 Reducer 2
        \       |       /
              |
              v
        Shuffle / Sort
       key -> [values]
              |
              v
            Reduce
              |
              v
          output.json
```

## 面试时怎么解释

“我先用 Python 实现了一个单机版 MapReduce 教学模型。Mapper 把输入记录转换成 key-value 中间结果；Partition 根据 key 决定目标 Reducer；Shuffle 阶段把相同 key 的 values 聚合到一起；Reducer 再做最终计算。它帮助我理解 MapReduce 的执行语义。与真实 Hadoop 的区别是，这个版本没有真正的多进程/多节点执行、网络 Shuffle、磁盘溢写和失败重试。”

## 下一步可以扩展

- 为每个 Mapper 添加运行日志和耗时统计。
- 把 Mapper 放进 `multiprocessing`，体验并行执行。
- 将中间结果写到磁盘，模拟 spill / merge。
- 为每个 task 增加失败重试和幂等提交。
- 把 `partition()` 改成哈希分区，并分析数据倾斜。
