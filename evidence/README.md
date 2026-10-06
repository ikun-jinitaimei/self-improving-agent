# 公开实验证据

此目录只接收已完成、同版本、同配置的真实配对实验，不放离线故障注入成绩。
结论与局限见 `docs/LIVE_RESULTS.md`。日期目录下的 dev / holdout 分开保存。

- `comparison.md`：恢复、退化、两策略正确率与额外计算量。
- `*-analysis.json`：分题型成绩、三次均通过的题数、缓存 token 和费用估算。
- `*-attempts.json`：所有计划尝试的身份、成绩、错误类型、调用量和耗时。
- `*-provenance.json`：完整调度、公开配置、源码/CSV 哈希和本地原件哈希。

`source_sha256` 对 manifest 中的源码文本快照按 UTF-8 编码计算（读取时换行
统一为 LF），不是 Windows CRLF 文件的原始字节哈希。CSV、manifest 与单次
轨迹原件的哈希按原始字节计算。

金额是公开价格快照计算的峰谷区间，不是实际账单。模型别名可能更新，温度
0 也不保证同样输出。相同题目的重复运行不算三个独立新任务。

完整消息、模型生成代码、包含标准答案的 manifest、凭据与本地绝对路径均
不在此目录；原始成功和失败轨迹保留在本地。哈希可核对原件是否变化，不能
仅凭公开哈希重建未公开的交互。这里的汇总不是独立第三方安全审计或认证。

重新评测：先准备受限 Docker 后端与本地 Key，再分别运行：

```shell
python demo.py run --policy both --split dev --repeats 3 --max-steps 6 --schedule-seed 0 --max-api-calls 432
python demo.py run --policy both --split holdout --repeats 3 --max-steps 6 --schedule-seed 0 --max-api-calls 432
```

不要为了复现旧成绩更改评分器或删除失败尝试。不要将运行目录整体上传到 Git。
