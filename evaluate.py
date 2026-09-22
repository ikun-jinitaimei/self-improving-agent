import json
from pathlib import Path
from typing import Any #这是python的类型注解，Any表示任何类型


# 所有路径都从当前脚本的位置出发，因此在其他目录运行脚本也能找到数据。
PROJECT_ROOT = Path(__file__).resolve().parent
TASKS_PATH = PROJECT_ROOT / "tasks" / "tasks.json"
PREDICTIONS_PATH = PROJECT_ROOT / "predictions" / "predictions.json"


def load_json_array(path: Path) -> list[dict[str, Any]]:  #path: Path 表示参数应该返回一个Path对象。后面部分表示返回值类型。最外层是列表，外面的大括号内表示字典，里面的大括号内表示键（字符串）值（任意数据类型）对。
    """读取顶层结构为列表的 JSON 文件。"""  #可以用help(load_json_array)进行查看
    with path.open("r", encoding="utf-8") as file:   #r表示只读，encoding="utf-8"表示使用utf-8编码读取文件。path.open()表示打开文件，返回一个文件对象。
        data = json.load(file)  #json.load()表示从文件中加载JSON数据，返回一个Python对象。

    # 任务和预测文件都约定使用列表作为最外层结构，格式不对时尽早报错。
    if not isinstance(data, list):   #isinstance()函数用于检查对象是否为指定类型。这里检查data是否为列表。
        raise ValueError(f"Expected a JSON array in {path}")  #如果data不是列表，则抛出ValueError异常，并打印错误信息。

    return data

def values_match(
    predicted_value: Any,
    reference_value: Any,
    numeric_tolerance: float,
) -> bool:
    """比较一个预测值与对应的标准参考值，返回 True 或 False。"""   #有点防御性编程了
    # Python 中 bool 是 int 的子类，所以必须显式排除，避免 True 被当成数值 1。
    reference_is_number = isinstance(reference_value, (int, float)) and not isinstance(
        reference_value, bool
    )
    predicted_is_number = isinstance(predicted_value, (int, float)) and not isinstance(
        predicted_value, bool
    )

    if reference_is_number:
        if not predicted_is_number:     #预测值和参考值都是数字类型才可以比较
            return False   #如果预测值不是数字类型，则返回False。
        difference = abs(predicted_value - reference_value)  #计算预测值和参考值的差值。
        return difference <= numeric_tolerance  #如果差值小于等于容忍度，则返回True，否则返回False。

    if isinstance(reference_value, str):
        if not isinstance(predicted_value, str):   #预测值和参考值都是字符串类型也可以比较
            return False
        # strip()忽略字符串首尾空格,casefold()做大小写标准化，减少无意义的格式错误。
        return predicted_value.strip().casefold() == reference_value.strip().casefold()

    # 列表、布尔值和 null 等其他 JSON 类型暂时使用精确比较。（已经比较了数值型和字符串型）
    return predicted_value == reference_value


def evaluate_answer(
    predicted_answer: dict[str, Any],
    reference_answer: dict[str, Any],
    numeric_tolerance: float,
) -> tuple[bool, dict[str, bool]]:  #返回值是元组，包括整题结果true/false和各字段结果字典field_results。
    """逐字段评估一道题，返回整题结果和各字段结果。"""
    field_results = {}

    # 以标准答案字段为准；预测答案缺少任何必需字段时，该字段直接判错。
    for field_name, reference_value in reference_answer.items():   #.items() 同时取得字典的键和值
        if field_name not in predicted_answer:
            field_results[field_name] = False
            continue

        field_results[field_name] = values_match(   #调用之前已经定义好的values_match()函数，比较预测值和参考值，true/false存储到field_results字典
            predicted_answer[field_name],     #注意这是字典，返回值是predicted_value
            reference_value,
            numeric_tolerance,
        )

    # 只有所有必需字段都正确，整道题才算通过True
    passed = all(field_results.values())   #all()函数用于检查字典的所有值是否都为True。field_results.values()展示存储的”值“
    return passed, field_results

# values_match()：判断一道题中的一个填空是否正确；
# evaluate_answer()：批改一整道题的所有填空；
# evaluate_tasks()：批改整张试卷上的所有题；
# print_report()：统计总分和正确率。

def evaluate_tasks(
    tasks: list[dict[str, Any]],  #tasks: list[dict[str, Any]] 表示tasks是一个列表，列表里面存储的是字典，字典里面存储的是键值对。
    predictions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按照 task_id 匹配任务与预测答案，然后评估全部任务。"""
    # 将列表转换成以 task_id 为键的字典，之后可以直接按 ID 查找答案。
    predictions_by_id = {
        prediction["task_id"]: prediction["answer"]
        for prediction in predictions
    }
    results = []

    for task in tasks:
        task_id = task["task_id"]
        evaluation = task["evaluation"]
        reference_answer = evaluation["reference_answer"]
        numeric_tolerance = evaluation["numeric_tolerance"]

        predicted_answer = predictions_by_id.get(task_id)  #get()函数用于获取字典中指定键的值。获取预测答案。后面进行比较

        # Agent 没有为某道题生成答案时，记录失败原因而不是让程序崩溃。
        if predicted_answer is None:
            results.append(
                {
                    "task_id": task_id,
                    "passed": False,
                    "field_results": {},
                    "error": "missing prediction",
                }
            )
            continue

        passed, field_results = evaluate_answer(
            predicted_answer,
            reference_answer,
            numeric_tolerance,
        )
        results.append(
            {
                "task_id": task_id,
                "passed": passed,
                "field_results": field_results,
                "error": None,
            }
        )

    return results


def print_report(results: list[dict[str, Any]]) -> None:
    """打印每道题的结果以及总体成功率。"""
    passed_count = 0

    for result in results:
        status = "PASS" if result["passed"] else "FAIL"
        print(f"[{status}] {result['task_id']}")
        #等价于
       # if result["passed"]:
            #status = "PASS"
        #else:
             #status = "FAIL"

        if result["passed"]:    #true/false
            passed_count += 1
        elif result["error"]:   #缺少预测
            print(f"  Error: {result['error']}")
        else:
            failed_fields = [     #存在预测，但部分字段错误。拼接成列表
                field_name   #field_name是字典的键
                for field_name, passed in result["field_results"].items() #.items() 同时取得字典的键和值
                if not passed   #如果值为false，则添加到failed_fields列表
            ]
            print(f"  Incorrect fields: {', '.join(failed_fields)}")

    total_count = len(results)  #results是包含所有题目最终结果的列表
    # 空任务集不能除以 0，因此此时将成功率定义为 0.0。
    success_rate = passed_count / total_count if total_count else 0.0
    print(f"\nSummary: {passed_count}/{total_count} passed ({success_rate:.1%})")    #同时输出分数和百分数。.1%表示保留一位小数，并转换为百分比。


def main() -> int:   #main() 不负责具体比较，而是负责调用其他函数：读取文件、执行评估、打印报告并返回退出码。
    """组织完整流程：读取文件、执行评估、打印报告并返回退出码。"""
    tasks = load_json_array(TASKS_PATH)
    predictions = load_json_array(PREDICTIONS_PATH)  #模拟AI模型agent生成的答案
    results = evaluate_tasks(tasks, predictions)  #批改整张试卷上的所有题
    print_report(results)

    # 全部通过时退出码为 0；存在失败时为 1，方便自动化测试或 CI 判断。
    return 0 if all(result["passed"] for result in results) else 1   #all()函数用于检查列表的所有元素是否都为True。passed是true/false。


# 直接运行本文件时才执行 main()；测试代码导入本文件时不会自动运行评估。
if __name__ == "__main__":   #作为模块导入时，__name__ 等于 "evaluate"。直接运行本文件时，__name__ 等于 "__main__"。
    raise SystemExit(main())   #result = main()。结果是0/1。raise SystemExit(result) 用于退出程序，并返回退出码。

#代码自底向上实现。但是先设计整体架构，自顶向下设计
