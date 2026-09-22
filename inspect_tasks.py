#检查任务文件，确保数据文件存在，并输出任务的ID、问题和数据文件名
import json
from pathlib import Path

project_root = Path(__file__).resolve().parent   #指向根目录
# __file__：当前 Python 文件的位置。
# Path(__file__)：将文件位置转换成路径对象。
# .resolve()：得到完整的绝对路径。
# .parent：取得当前文件所在的文件夹
tasks_path = project_root / "tasks" / "tasks.json"  #拼接路径
#print(tasks_path)

with tasks_path.open("r", encoding="utf-8") as file:  #以只读方式打开文件
    tasks = json.load(file)  #将json文件加载为python对象
#print(type(tasks))  #python列表形式
#print(len(tasks))

for task in tasks:   #一共有三个任务，循环遍历
    task_id = task["task_id"]
    agent_input = task["input"]

    print(f"Task ID: {task_id}")
    print(f"Question: {agent_input['question']}")
    print(f"Data file: {agent_input['data_file']}")
    print()  #只打印一个空行，用来分隔不同任务的输出，便于阅读