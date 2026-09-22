"""测试 evaluate.py 中最核心的判分逻辑。

这个文件测试的是“评估器代码是否正确”,而不是“Agent 是否答对任务”。
文件名以 test_ 开头，便于 unittest 自动发现其中的测试。
"""

import unittest

# 从项目根目录的 evaluate.py 导入真正需要测试的函数。
# 导入时不会执行 evaluate.py 的 main()，因为此时 evaluate.__name__ 不是 "__main__"。
from evaluate import evaluate_answer, values_match


# 继承 unittest.TestCase 后，这个类可以使用 assertTrue、assertFalse 等断言方法。
class ValuesMatchTests(unittest.TestCase):
    def test_number_within_tolerance_passes(self):   #按照unittest框架规则约定命名，以test开头的方法都会被调用
        """数值误差没有超过 tolerance 时，应该判定为正确。"""
        # 预测值与标准值相差约 0.005，小于允许误差 0.01。
        # assertTrue 表示我们预期 values_match() 返回 True。  举一个明确的样例，断言行为是否符合预期，不追求泛化
        self.assertTrue(values_match(42200.005, 42200.0, 0.01))

    def test_number_outside_tolerance_fails(self):
        """数值误差超过 tolerance 时，应该判定为错误。"""
        # 预测值与标准值相差约 0.02，大于允许误差 0.01。
        # assertFalse 表示我们预期 values_match() 返回 False。
        self.assertFalse(values_match(42200.02, 42200.0, 0.01))

    def test_string_comparison_ignores_case_and_outer_spaces(self):
        """字符串比较应该忽略首尾空格和英文大小写。"""
        # values_match() 会先对字符串调用 strip() 去掉收尾空格和 casefold()统一大小写。
        # 因此 " south " 和 "South" 规范化后都是 "south"。
        # tolerance 只用于数值；这里虽然需要传入参数，但不会参与字符串比较，不会看这个
        self.assertTrue(values_match(" south ", "South", 0.0))

    def test_missing_required_field_fails(self):
        """预测答案缺少标准答案要求的字段时，整道题应该失败。"""
        # 模拟假设 Agent 只回答了地区，却遗漏了必需的 sales_amount 字段。
        # evaluate_answer() 返回两个值：整题是否通过，以及每个字段是否通过。
        passed, field_results = evaluate_answer(
            predicted_answer={"region": "South"},
            reference_answer={"region": "South", "sales_amount": 23478.0},
            numeric_tolerance=0.01,
        )

        # 第一个断言，检查整道题的最终结果是 False。
        self.assertFalse(passed)   #field_results都是true，passed才是true
        # 第二个断言，进一步确认失败原因确实是 sales_amount 字段缺失。
        self.assertFalse(field_results["sales_amount"])


# 直接执行 `python tests/test_evaluate.py` 时启动 unittest。实际不能成功，因为导入的函数在根目录中
# 使用 `python -m unittest discover -s tests -v` 时，unittest 也会自动发现这些测试。
# python -m unittest 让 Python 运行自己内置的 unittest 测试工具。先从当前项目根目录/当前工作目录启动测试系统
# discover意思是“自动寻找测试文件”。
# -s 是 start directory，表示：从 tests 文件夹开始寻找测试。
# -v 是 verbose，表示显示详细信息。测试方法名 测试说明 ... ok
# ok 表示该测试的所有断言都满足预期
if __name__ == "__main__":
    unittest.main()   #当前类继承了unittest.TestCase，指明父类可以能直接运行子类的test开头的方法