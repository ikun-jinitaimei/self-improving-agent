# Project collaboration instructions

## Purpose

Self-Improving Agent is an AI / Agent research and engineering project.
Study how tool interactions, execution feedback, verification, trajectories,
memory and eventually post-training or RL can improve task performance.
Keep methods general enough to evaluate across data analysis, coding and other environments.

## Current stage

This independent v0.2.2 research candidate adds Docker execution, reproducible
dev/holdout experiments, explicit verification, session events and cost analysis.
The original V0 workspace and commit d40a3b2 remain unchanged.
On 24 synthetic CSV task IDs repeated three times per policy, real DeepSeek runs
completed 144 attempts: dev baseline/verify 36/36 and 36/36; frozen holdout 36/36
and 35/36. The verification failure exhausted the six-request budget.
Verification used more computation without an observed accuracy benefit, so
baseline remains the default. See docs/LIVE_RESULTS.md and evidence/.
These results are not evidence of cross-domain generalization or learned
self-improvement; no memory learning, parameter training or RL is implemented.

The runtime follows:
task input → model decision → tool execution → observation → next decision → final answer.
The experiment runner saves trajectories and evaluates answers after execution.

## Working style

- Implement one coherent component at a time, then explain and verify it.
- Write detailed Chinese comments and docstrings for new or changed Python logic.
- Explain inputs, outputs, important design choices and non-obvious Python patterns.
- Keep conversational explanations concise unless a detailed walkthrough is requested.
- Distinguish software-engineering, LLM, Agent and RL concepts.
- Prefer explicit, readable Python and visible mechanisms over framework abstractions.
- Keep code concise: validate external inputs once at a clear boundary, and let internal
  programming errors surface instead of masking them with broad exception handlers.
- Keep responsibilities separate and edits within the requested scope; preserve detailed
  comments explaining decisions without redundant line-by-line narration.
- Preserve existing user changes; verify edits by reading the saved files.

## Research and evaluation

No self-improvement claim without evaluation.
Inspect current code, experiments and results before proposing the next step.
Choose work through hypothesis → implementation → experiment → analysis → next hypothesis.

Prefer verifiable tasks and deterministic evaluators. Never fabricate experiment results.
Keep reference answers out of model inputs and distinguish prompt separation from OS isolation.
Record success rate, correctness, steps, tool calls, execution failures, token usage and latency.
Preserve failed trajectories as well as successful ones.
Separate mock tests, hand-written predictions and actual model runs in reporting.
Compare improvements with a baseline and account for additional computation and cost.

## Engineering principles

- Start with simple architecture, modular functions and explicit state.
- Keep experiments traceable with configuration, logs and meaningful tests.
- Justify major dependencies and architectural changes by a concrete problem and evaluation.
- Avoid premature frameworks, multi-agent orchestration, UI, RAG or memory features.
- Do not expand the system merely to add features or produce a polished demo.
- Local Python execution is not an OS sandbox. Real candidate runs default to the
  restricted Docker backend; boundary tests do not establish production safety.
  Review SECURITY.md before untrusted or multi-user use.
- Keep credentials, local personal notes, virtual environments and unreviewed logs out of Git.
- Before publishing, inspect the exact files and repository history for private content.

## Progression

1. Stable minimal Agent loop and tool execution.
2. Reproducible evaluation and trajectory analysis.
3. Hypothesis-driven verification and reflection experiments.
4. Planning, context or memory only when evidence motivates them.
5. Learning from selected trajectories and small-model post-training.
6. RL / RLVR and iterative improvement, evaluated quantitatively.

Prioritize clear experimental evidence, ablations and documented limitations.
