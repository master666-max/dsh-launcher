# dsh-launcher

**DeepSeek Harness (dsh) 的独立启动与运维工具链。**

不依赖任何 AI 工作台 / IDE 插件，也不依赖 dsh 源码的内部结构 ——
仓库路径、profile、端口、启动命令全部在运行时探测。

---

## 快速开始

1. 把本目录放到 `%USERPROFILE%\dsh-launcher\`
2. 把 `start-dsh.bat` 复制到桌面（或自建快捷方式）
3. 双击它 → **1 秒内不做任何输入** 自动启动 dsh；**按任意键** 进入菜单

菜单：

```
[1] 启动 dsh
[2] 插件管理（冲突检查 / 启用禁用）
[3] 只检查插件冲突
[4] 补齐模块链接
[5] 环境探测报告
[0] 退出
```

## 文件说明

| 文件 | 职责 |
| --- | --- |
| `dsh_env.py` | **探测层（单例）**。唯一认识 dsh 的模块：仓库定位、profile 定位、端口探活、启动命令探测、权威插件树、进程与锁管理 |
| `dsh-launcher.py` | 编排 + 交互菜单。**不含任何 dsh 内部知识** |
| `dsh-plugins.py` | 插件管理：列出全部入口、冲突检查、启用/禁用（写 profile 补丁） |
| `dsh-fallback-heal.py` | 补齐 `.dsh-module-fallback` 缺失 junction |
| `dsh_tests.py` | **回归测试（29 用例）**，写操作全在临时副本上 |
| `dsh-selfcheck.py` | 静态自检：语法 / 缺失 import / 未定义名 / GBK 字符 / subprocess 参数名 / bat 行尾 / 解耦 |
| `dsh-env.py` | 命令行薄壳（`--dump` / `--refresh` / `--json`） |
| `MAINTENANCE.md` | **维护与审计说明**（故障速查、审计要点、改动纪律） |

## 改完代码后必做

```bat
python dsh-selfcheck.py      :: 静态自检，0 问题才算完
python dsh_tests.py          :: 29 用例回归，全过才算完
python dsh-env.py --refresh  :: dsh 升级或换目录后强制重探
```

## 环境要求

- Windows
- **Python 3.8+**（入口 bat 会自动查找；也可用环境变量 `DSH_PYEXE` 指定）
- Node.js + pnpm（dsh 自身需要）

## 设计约束

- **零硬编码路径**：所有路径从 `__file__` 推导，整个目录可以随意搬动
- **零 agent 依赖**：不引用、不读取任何 AI 工作台 / IDE 插件的目录或环境变量
  （自检第 8 项会复核这一点）
- **不硬崩**：dsh 破坏性更新后探测不到就降级，并在报告里说明
- **破坏性操作需显式许可**：删链接要 `--clean`，杀进程要 `--kill`

## 运行时缓存（不入库）

以下文件会自动重建，`.gitignore` 已排除：

- `dsh-env.cache.json` —— 上次探测结果
- `dsh-dump.cache.txt` (+ `.meta.json`) —— 权威插件树缓存

它们包含**本机真实路径**，请勿提交到公开仓库。
