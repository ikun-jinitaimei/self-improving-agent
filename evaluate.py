"""确定性评估器：逐字段比较答案，并按 task_id 汇总整批成绩。

values_match 比较一个值，evaluate_answer 评估一道题，
evaluate_tasks 处理一批题，print_report 只负责展示结果。
这些函数不调用模型，也不执行工具。
"""

import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
TASKS_PATH = PROJECT_ROOT / "tasks" / "tasks.json"
PREDICTIONS_PATH = PROJECT_ROOT / "predictions" / "predictions.json"


def load_json_array(path: Path) -> list[dict[str, Any]]:
    """读取顶层为列表的 JSON。path: Path 是参数类型，箭头后才是返回类型。"""
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    # 文件读取是输入边界：顶层不是列表时明确报错，避免后续迭代产生误导。
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array in {path}")
    return data


def values_match(
    predicted_value: Any,
    reference_value: Any,
    numeric_tolerance: float,
) -> bool:
    """按标准值的类型选择规则：数字用绝对误差，字符串忽略首尾空格和大小写。"""
    # bool 是 int 的子类，需要排除，防止 True 被当成数值 1。
    reference_is_number = isinstance(reference_value, (int, float)) and not isinstance(
        reference_value, bool
    )
    predicted_is_number = isinstance(predicted_value, (int, float)) and not isinstance(
        predicted_value, bool
    )
    if reference_is_number:
        if not predicted_is_number:
            return False
        return abs(predicted_value - reference_value) <= numeric_tolerance
    if isinstance(reference_value, str):
        if not isinstance(predicted_value, str):
            return False
        return predicted_value.strip().casefold() == reference_value.strip().casefold()
    # 其他 JSON 值沿用 Python 等值比较；当前任务只使用数字和字符串字段。
    return predicted_value == reference_value


def evaluate_answer(
    predicted_answer: dict[str, Any],
    reference_answer: dict[str, Any],
    numeric_tolerance: float,
) -> tuple[bool, dict[str, bool]]:
    """返回整题是否通过，以及每个必需字段的判分结果。"""
    field_results = {}
    for field_name, reference_value in reference_answer.items():
        # 缺失字段直接判错；不会因为只提交部分正确答案而通过整题。
        if field_name not in predicted_answer:
            field_results[field_name] = False
            continue
        field_results[field_name] = values_match(
            predicted_answer[field_name], reference_value, numeric_tolerance
        )
    # all 要求每个字段都正确；返回元组可以由调用方解包成两个变量。
    return all(field_results.values()), field_results


def evaluate_tasks(
    tasks: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按任务 ID 匹配预测；没有答案的题也保留在结果中，不能漏出失败分母。"""
    predictions_by_id = {
        prediction["task_id"]: prediction["answer"] for prediction in predictions
    }
    results = []
    for task in tasks:
        task_id = task["task_id"]
        evaluation = task["evaluation"]
        predicted_answer = predictions_by_id.get(task_id)
        if predicted_answer is None:
            results.append({
                "task_id": task_id, "passed": False,
                "field_results": {}, "error": "missing prediction",
            })
            continue
        passed, field_results = evaluate_answer(
            predicted_answer,
            evaluation["reference_answer"],
            evaluation["numeric_tolerance"],
        )
        results.append({
            "task_id": task_id, "passed": passed,
            "field_results": field_results, "error": None,
        })
    return results


def print_report(results: list[dict[str, Any]]) -> None:
    """展示逐题结果和通过比例；保留字段失败与缺失答案的区别。"""
    passed_count = 0
    for result in results:
        status = "PASS" if result["passed"] else "FAIL"
        print(f"[{status}] {result['task_id']}")
        if result["passed"]:
            passed_count += 1
        elif result["error"]:
            print(f"  Error: {result['error']}")
        else:
            failed_fields = [
                name for name, passed in result["field_results"].items() if not passed
            ]
            print(f"  Incorrect fields: {', '.join(failed_fields)}")
    total_count = len(results)
    success_rate = passed_count / total_count if total_count else 0.0
    print(f"\nSummary: {passed_count}/{total_count} passed ({success_rate:.1%})")


def main() -> int:
    """评估仓库中的手写示例；真实 Agent 实验由 run_benchmark.py 组织。"""
    tasks = load_json_array(TASKS_PATH)
    predictions = load_json_array(PREDICTIONS_PATH)
    results = evaluate_tasks(tasks, predictions)
    print_report(results)
    # 退出码供终端或自动化脚本判断：0 表示全部通过，1 表示有题失败。
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
