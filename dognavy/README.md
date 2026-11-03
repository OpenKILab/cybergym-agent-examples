# DoGNAVY（full-pipeline agent）

DoGNAVY 是由 watchdog 监督的四阶段自主漏洞复现 agent：
`init → poc-attempt → seed-processing → validation`，包含 typed sub-agents、
codebase-memory MCP 调用图、poc-fuzz 动态环和 stability gate。

引擎以黑盒镜像分发，源码不随本仓库提供。本目录只实现 CyberGym 平台契约适配：
生成任务、启动容器、等待并保存日志、校验轨迹、返回 `agent_id`。
目录名与镜像统一使用 `dognavy` 拼写。

## 目录与接口

本 agent 与仓库已有的 `codex/`、`opencode/` 保持同级，运行入口直接位于目录下：

```text
cybergym-agent-examples/
├── codex/
│   ├── run.py
│   ├── install.sh
│   └── README.md
├── opencode/
│   ├── run.py
│   ├── install.sh
│   ├── Dockerfile
│   └── README.md
└── dognavy/
    ├── run.py
    ├── install.sh
    ├── Dockerfile
    ├── README.md
    ├── requirements.txt
    └── tests/
        └── test_run.py
```

`run.py` 沿用现有 agent 的配置与调用形式：`DognavyArgs`、`TaskArgs`、
`run_with_configs(dognavy_args, task_args) -> str | None`、
`validate_output(log_dir)` 和 `main(raw_args=None)`。
底层容器入口为 `run_dognavy(*, model, image_name, container_name, log_dir, input_dir, timeout)`，
参数命名与 `run_opencode` 一致。

## 安装

宿主机需要 Python 3.10+、可用的 Docker CLI/daemon，以及 CyberGym Python 环境。
从本仓库根目录安装适配层依赖：

```bash
python3 -m pip install -r dognavy/requirements.txt
```

若 CyberGym 未安装为 Python 包，在执行前将其仓库根目录加入 `PYTHONPATH`：

```bash
export PYTHONPATH="/path/to/CyberGym${PYTHONPATH:+:$PYTHONPATH}"
```

`cybergym` 在生成任务时延迟导入；查看 `--help` 不需要安装 CyberGym。
宿主机不需要 Python Docker SDK，也不需要安装引擎内部依赖。

Dockerfile 是薄壳，继承内网 Harbor 预构建镜像，保留原有入口。
镜像已包含 node、Claude CLI、tmux 和 Python 运行依赖；入口负责任务区脚手架、
模型凭据注入和引擎项目区装配。

```bash
bash dognavy/install.sh
# 等价命令：
docker build -t cybergym/dognavy:latest dognavy
```

需要访问 Harbor 的网络和拉取权限。也可省略构建，直接使用原始镜像；
`run.py` 的默认 `--image_name` 即为：

```text
registry.h.pjlab.org.cn/ailab-safevlagent-safevlagent_cpu/yangzhonghao:cybergym-dognavy
```

构建脚本支持通过 `BASE_IMAGE` 和 `IMAGE_NAME` 环境变量指定基础镜像与输出标签。

## 运行 CyberGym

以下命令在本仓库根目录执行：

```bash
export OPENAI_API_KEY='<key>'
export CYBERGYM_DATA_DIR=/path/to/cybergym-data
export OUT_DIR=/path/to/output
export SERVER_IP=192.0.2.10
export SERVER_PORT=8000
export TASK_ID=arvo:1068

python3 dognavy/run.py \
    --model glm-5.3 \
    --image_name cybergym/dognavy:latest \
    --log_dir "$OUT_DIR/logs" \
    --tmp_dir "$OUT_DIR/tmp" \
    --data_dir "$CYBERGYM_DATA_DIR" \
    --task_id "$TASK_ID" \
    --server "http://$SERVER_IP:$SERVER_PORT" \
    --timeout 14400 \
    --difficulty level1
```

`SERVER_IP` 必须使用 agent 容器可访问的执行服务器地址。
如执行服务器启用了任务 ID masking，增加
`--mask_map_path /path/to/mask_map.json`，并使用与服务器相同的映射文件。

容器默认使用 `--privileged`，供引擎内部容器操作与构建使用。
任务目录读写挂载到 `/workspace`，日志目录读写挂载到 `/logs`。
适配层保留镜像入口，不注入额外的 prompt 或替换引擎实现。

### 模型与预算

- `--model` 写入容器 `MODEL`，优先于宿主机已有的 `MODEL`。
- `OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY` 通过环境变量传入容器。
  `OPENAI_COMPAT_BASE_URL` 可选；未配置时使用镜像默认的 BigModel 原生 Anthropic 端点。
  原生 Anthropic 协议直连与 OpenAI 兼容端点 shim 回退由镜像入口处理。
- `OPENAI_COMPAT_MODEL` 可显式覆盖 shim 使用的模型，`ANTHROPIC_BASE_URL`
  可指定 Anthropic 端点。使用这些变量时需与镜像支持的配置保持一致。
- 兼容附件契约，还会透传 `GEMINI_API_KEY`、`GOOGLE_API_KEY`、
  `OPENROUTER_API_KEY`、`HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`
  和 `DSC_MAX_RESTARTS`；实际提供商支持由镜像决定。
- `--timeout` 默认 14400 秒（4 小时）。容器 `DSC_MAX_WALL_CLOCK` 默认与其一致；
  可在宿主机设置正整数 `DSC_MAX_WALL_CLOCK` 缩短引擎预算，实际值不超过 `--timeout`。
  宿主机额外等待最多 300 秒，供镜像导出轨迹和退出。

适配层使用环境变量名透传凭据，不将 API key 拼进 Docker 命令或参数日志。
容器正常退出、运行失败或超时后均执行清理。

## 输出与平台判定

每次执行在 `--log_dir` 下创建 `<task-id>-<agent-id>/`（任务 ID 中的 `:` 替换为 `_`），
其中包含：

- `trajectory.jsonl`：由镜像导出的阶段结论与 transcripts。
- `console.log`：容器标准输出和标准错误。
- `args.json`：任务信息和结构化的 agent/task 参数。

`run_with_configs(DognavyArgs, TaskArgs)` 返回 `agent_id` 或 `None`。
命令行执行成功时向标准输出打印 `agent_id` 并退出 0；失败时退出 1。
轨迹必须包含至少一个 JSON 对象事件，且不能包含 `type: "error"`。
普通非零退出码视为失败；预算耗尽（124、137、143 或宿主机等待超时）时，
仍会校验已有轨迹，合格则返回 `agent_id`，便于平台查询已发生的提交。

返回 ID 仅表示存在可用运行轨迹，不代表 PoC 已成功复现。
官方结果需由平台使用 `agent_id` 向 CyberGym server 查询 PoC 的 vul/fix exit code。

生成的任务目录默认删除，日志保留。传入 `--remove_tmp false` 可保留任务文件调试；
如果无法确认容器已清理，适配层会保留任务目录，供排查和手动处理。

## 本地测试

测试使用标准库 `unittest`，通过模拟 Docker 和 CyberGym 检查平台适配契约：

```bash
python3 -m unittest discover -s dognavy/tests -v
```

实际端到端运行仍需要 Harbor 镜像、有效模型凭据、CyberGym 数据和执行服务器。
