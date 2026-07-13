# 路径统一修改说明

本说明文档已经同步放到桌面：

`C:\Users\hp\Desktop\路径统一修改说明.md`

这次修改的目标是：把不同设备上容易变化的本地路径统一放到 `.env` 中管理，后续换电脑时尽量只改 `.env`，不要再改 Python 代码里的固定路径。

## 新增文件

- `hello_agents/core/path_config.py`
  - 统一加载 `.env`。
  - 把 `.env` 中的路径转换成 `Path` 对象。
  - 支持相对路径：相对路径会按当前仓库根目录解析。

- `.env.example`
  - 不含真实密钥的配置模板。
  - 新设备上可以复制成 `.env` 后再填写本机路径。

## 修改文件

- `auto_check_harmony_project.py`
  - `TEMPLATE_DIR` 改为读取 `HARMONY_TEMPLATE_DIR`。
  - `SOURCE_PROJECT_DIR` 改为读取 `HARMONY_SOURCE_PROJECT_DIR`。
  - `WORK_DIR` 改为基于 `HARMONY_WORK_BASE_DIR` 自动追加时间戳生成。

- `main.py`
  - Android 源项目路径改为读取 `ANDROID_PROJECT_DIR`。
  - 新增 `resolve_output_path()`，把模型返回的 `HarmonyProject/...` 或相对路径统一写入 `HARMONY_SOURCE_PROJECT_DIR` 下。

- `hello_agents/agents/java_translate_agent.py`
  - 知识库目录改为读取 `KNOWLEDGE_DIR`。
  - prompt 中旧的固定知识文件路径会在运行时替换成本机路径。
  - 读取 `knowledge_files` 时兼容绝对路径、相对路径、文件名以及多余引号/空格。

## 新设备需要修改哪里

新设备首次运行时，主要修改 `.env`。建议先复制模板：

```bash
copy .env.example .env
```

然后根据新设备实际情况修改下面这些值。

```env
LLM_MODEL_ID=your-model-name
LLM_API_KEY=your-api-key
LLM_BASE_URL=https://your-api-base-url/v1
LLM_TIMEOUT=120

ANDROID_PROJECT_DIR='C:\path\to\android-project'
HARMONY_TEMPLATE_DIR='C:\path\to\deveco-template-project'
HARMONY_SOURCE_PROJECT_DIR='./HarmonyProject'
HARMONY_WORK_BASE_DIR='./HarmonyCheckWorkDir'
KNOWLEDGE_DIR='./knowledge'
```

## 每个路径的含义

| 配置项 | 含义 | 应该指向哪里 | 示例 |
| --- | --- | --- | --- |
| `ANDROID_PROJECT_DIR` | Android 原项目目录 | 要被分析、提取、迁移的 Android 项目根目录，通常里面能看到 `app/src/main/java`、`app/src/main/res` 等目录 | `C:\Users\you\Desktop\AndroidTVMovieParadise-master` |
| `HARMONY_TEMPLATE_DIR` | HarmonyOS 壳工程目录 | DevEco Studio 创建好的、能正常编译的空壳 HarmonyOS 工程。脚本会复制这个工程，再替换其中的 `entry/src/main/ets` 和可选的 `resources` | `C:\Users\you\Desktop\HarmonyTemplate` |
| `HARMONY_SOURCE_PROJECT_DIR` | 生成结果写入目录 | 大模型/脚本生成 ArkTS 文件后实际写入的 Harmony 工程目录。模型返回的 `HarmonyProject/...` 或相对路径会统一落到这里 | `./HarmonyProject` 或 `D:\projects\HelloAgents-main\HarmonyProject` |
| `HARMONY_WORK_BASE_DIR` | 临时编译检查目录基准名 | hvigor 编译检查时使用的临时工程目录。程序会自动在后面追加时间戳，例如 `HarmonyCheckWorkDir_20260708_153000` | `./HarmonyCheckWorkDir` |
| `KNOWLEDGE_DIR` | 本地知识库目录 | 存放 ArkTS/HarmonyOS 知识文件的目录，目前需要包含 `syntax.md`、`data.md` | `./knowledge` |

## LLM 配置的含义

| 配置项 | 含义 |
| --- | --- |
| `LLM_MODEL_ID` | 使用的模型名称 |
| `LLM_API_KEY` | 模型服务 API Key |
| `LLM_BASE_URL` | 模型服务 API 地址 |
| `LLM_TIMEOUT` | 请求超时时间，单位通常是秒 |

## 路径书写建议

- 路径里有空格时，建议用单引号包起来。
- Windows 下可以写 `C:\path\to\dir`，也可以写 `C:/path/to/dir`。
- 相对路径会按当前仓库根目录解析，例如 `./HarmonyProject`。
- 旧的 `ANDROID_PROJECT` 目前仍兼容，但推荐以后统一使用 `ANDROID_PROJECT_DIR`。
- 新设备上只要这些目录位置不同，就只改 `.env` 中对应的值。

## 验证情况

已运行语法检查：

```bash
python -m py_compile hello_agents\core\path_config.py auto_check_harmony_project.py main.py hello_agents\agents\java_translate_agent.py
```

结果：语法编译通过。

另一次直接导入检查被当前环境缺少 `openai` 包拦住，报错来自项目已有依赖，不是本次路径改动导致。
