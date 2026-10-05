"""可复现的合成 CSV 基准：数据生成、题目定义和标准答案都可审阅。

这是受控的数据分析基准，不是公开排行榜。dev 用于开发，holdout 使用不同
随机种子；同一题型仍然共享生成规则，因此不能把 holdout 称为未知领域泛化。
标准答案只交给实验组织者，不能放进模型消息或数据挂载目录。
"""

import csv
import hashlib
import json
import random
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


SUITE_VERSION = "csv-v1"
SEEDS = {"dev": (7, 23, 91), "holdout": (101, 211, 307)}
CATEGORIES = ("region", "monthly", "missing", "weighted")


def task_ids(split: str) -> set[str]:
    """返回可用任务身份，不写文件；CLI dry-run 与筛选校验可以先于 API 请求。"""
    groups = SEEDS if split == "both" else {split: SEEDS[split]}
    return {f"task_{group}_{seed}_{category}" for group, seeds in groups.items()
            for seed in seeds for category in CATEGORIES}


def money(value: Decimal) -> float:
    """标准答案以 Decimal 计算并保留两位，避免生成器本身引入浮点累计误差。"""
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def generate_rows(seed: int) -> list[dict]:
    """用局部随机数实例生成 40 行；不改变其他代码的全局 random 状态。

    每份文件包含三个自然月、退货负数、地区首尾空格和三种缺失表示。
    这些边界条件让题目不只是复制第一版 sales.csv 的固定公式。
    """
    rng = random.Random(seed)
    rows = []
    for index in range(40):
        region = rng.choice(["North", "South", "East", "West"])
        product = rng.choice(["Laptop", "Keyboard", "Monitor", "Mouse"])
        rows.append({
            "date": f"2026-{index % 3 + 1:02d}-{index % 27 + 1:02d}",
            "region": f" {region} " if index % 5 == 0 else region,
            "product": ("", "NA", "null")[index % 3] if index % 7 == 0 else product,
            "quantity": str(-rng.randint(1, 3) if index % 9 == 0 else rng.randint(1, 12)),
            "unit_price": f"{rng.randint(100, 50000) / 100:.2f}",
        })
    return rows


def reference_answers(rows: list[dict]) -> dict[str, dict]:
    """独立使用 Decimal 推导答案；演示工具代码使用 float，避免共享同一实现。

    加权均价 = 总销售额 / 净数量，退货参与分子、分母。最大地区若并列，按
    字母序选择一个。缺失值只统计 product 中空白、NA、null（忽略大小写）。
    """
    regions = defaultdict(Decimal)
    monthly = Decimal(0)
    total, quantity = Decimal(0), 0
    missing = 0
    for row in rows:
        amount = Decimal(row["quantity"]) * Decimal(row["unit_price"])
        regions[row["region"].strip()] += amount
        total += amount
        quantity += int(row["quantity"])
        if row["date"].startswith("2026-01"):
            monthly += amount
        missing += row["product"].strip().casefold() in {"", "na", "null"}
    best = sorted(regions, key=lambda name: (-regions[name], name))[0]
    return {
        "region": {"region": best, "sales_amount": money(regions[best])},
        "monthly": {"year_month": "2026-01", "sales_amount": money(monthly)},
        "missing": {"column": "product", "missing_count": missing},
        "weighted": {"weighted_unit_price": money(total / quantity)},
    }


def build_suite(directory: Path, split: str = "dev") -> list[dict]:
    """在新目录写入数据，返回含私有评分信息的任务列表。

    directory 只能包含 CSV；调用者把 manifest/标准答案写在其父目录。每次
    实验新建目录，不覆盖历史数据。both 包含 24 题，其余各 12 题。
    """
    if split not in {"dev", "holdout", "both"}:
        raise ValueError("split 必须是 dev、holdout 或 both")
    directory.mkdir(parents=True, exist_ok=False)
    groups = SEEDS if split == "both" else {split: SEEDS[split]}
    tasks = []
    questions = {
        "region": "Which region has the highest total quantity * unit_price? Strip region whitespace; include returns. Break ties alphabetically.",
        "monthly": "What is total quantity * unit_price for January 2026? Include negative quantities (returns).",
        "missing": "How many product values are missing? Treat blank, NA and null as missing, case-insensitively after stripping whitespace.",
        "weighted": "Calculate weighted unit price: sum(quantity * unit_price) / sum(quantity), including returns. Round to two decimals.",
    }
    for group, seeds in groups.items():
        for seed in seeds:
            rows = generate_rows(seed)
            filename = f"sales_{group}_{seed}.csv"
            with (directory / filename).open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            for category, answer in reference_answers(rows).items():
                answer_format = {
                    key: "integer" if isinstance(value, int) else
                    "number" if isinstance(value, float) else "string"
                    for key, value in answer.items()
                }
                tasks.append({
                    "task_id": f"task_{group}_{seed}_{category}",
                    "category": category, "split": group,
                    "input": {"question": questions[category], "data_file": filename,
                              "answer_format": answer_format},
                    "evaluation": {"reference_answer": answer, "numeric_tolerance": 0.01},
                })
    return tasks


def suite_fingerprint(tasks: list[dict], data_root: Path) -> str:
    """把任务定义与文件内容一起摘要；比较实验时据此拒绝不同题目或数据。"""
    digest = hashlib.sha256(json.dumps(tasks, sort_keys=True, ensure_ascii=False).encode())
    for name in sorted({task["input"]["data_file"] for task in tasks}):
        digest.update(name.encode())
        digest.update((data_root / name).read_bytes())
    return digest.hexdigest()
