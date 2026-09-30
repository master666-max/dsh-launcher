# dsh-launcher

**DeepSeek Harness (dsh) 的独立启动与运维工具链。**

不依赖任何 AI 工作台 / IDE 插件，也不依赖 dsh 源码的内部结构 ——
仓库路径、profile、端口、启动命令全部在运行时探测。

## 亮点

| 能力 | 说明 |
| --- | --- |
| 🚀 **后台启动** | dsh 拉起后终端交还菜单；输出落 `~/.dsh/logs/`，报错不瞎 |
| 🛡️ **安全模式启动** | 插件把 dsh 搞挂时的最小集逃生门 —— 只禁外部插件、内置不动、字节级备份、一键还原 |
| 🔁 **一键重启** | 结束实例 → 等端口真释放 → 走完整启动流程（防脏锁） |
| 🔍 **运行时探测** | 仓库 / profile / 端口 / 启动命令全探测，零硬编码路径，目录随意搬 |
| 🧪 **变异测试** | 21 个故意改坏的关键逻辑点，验证回归用例「真能抓到」而非只在干净代码上全绿 |
| 🩺 **六类故障速查** | 启动失败按层排查（PATH 抢占 → 端口排除范围），方法论沉淀在 [MAINTENANCE.md](MAINTENANCE.md) |

---

## 快速开始

**本仓库是唯一发布源**，直接 clone 即可（所需文件全部在仓库内，无外部依赖）：

```bat
git clone https://github.com/master666-max/dsh-launcher.git "%USERPROFILE%\dsh-launcher"
```

1. 上面这条命令把工具链放到 `%USERPROFILE%\dsh-launcher\`
2. 把仓库里的 **`start-dsh.bat`** 复制到桌面；
   `结束-dsh.bat`（停止脚本）建议也一起复制
3. 双击 `start-dsh.bat` → **1 秒内不做任何输入** 自动启动 dsh；**按任意键** 进入菜单

> **工具链目录怎么找**（2026-09-26 起不再硬编码）：`start-dsh.bat` 按
> ① 环境变量 `DSH_TOOLS` → ② bat 自己所在目录 → ③ `%USERPROFILE%\dsh-launcher`
> 的顺序找 `dsh-launcher.py`，三处都没有才报错（并列出找过的位置）。
> 所以**整个目录搬到哪都能用**：把 bat 放进工具链目录即可，或者设
> `DSH_TOOLS` 指过去；桌面副本则走 ③ 这个默认位。

> 更新：`cd %USERPROFILE%\dsh-launcher && git pull`
> ⚠️ 仓库用 `.gitattributes` 把 `*.bat` 钉死为 **CRLF** ——
> 这是必须的（裸 LF 的 bat 双击会秒退），别删那条规则。
>
> ⚠️ **桌面上的 `start-dsh.bat` / `结束-dsh.bat` 只是副本。**
> 要改启动逻辑请改**仓库里那份**，再复制到桌面。
> 反过来改（只改桌面）会 drift —— 下次 `git pull` 覆盖不回来，
> 而且换台机器就丢了。**仓库是唯一真相，桌面只是入口。**

菜单：

```
[1] 启动 dsh
[2] 插件管理（冲突检查 / 启用禁用）
[3] 只检查插件冲突
[4] 补齐模块链接
[5] 环境探测报告
[6] 安全模式启动（禁用全部外部插件）
[7] 恢复正式模式（还原安全模式禁掉的插件）
[8] 重启 dsh（结束现有实例再启动）
[0] 退出
```

> [6]/[7] 类比 Windows 安全模式：某个外部插件把 dsh 搞挂时，[6] 仍能以最小插件集进系统排查。
> 进入前必须拿到权威插件树（入口 id ≠ 包名，拿不到就拒绝，绝不假安全）；
> 原补丁字节级备份，[7] 一键原样还原，全程可逆。

> **dsh 在后台运行**（2026-09-30 起）：[1]/[6]/[8] 拉起 dsh 后本终端回到菜单，
> dsh 的输出落 `~/.dsh/logs/dsh-bg-*.log`（保留最近 10 份）—— 报错先看它。
> 停止/重启 dsh：菜单 [8] 或桌面 结束-dsh.bat。

## 文件说明

| 文件 | 职责 |
| --- | --- |
| `dsh_env.py` | **探测层（单例）**。唯一认识 dsh 的模块：仓库定位、profile 定位、端口探活、启动命令探测、权威插件树、进程与锁管理 |
| `dsh-launcher.py` | 编排 + 交互菜单。**不含任何 dsh 内部知识** |
| `dsh-plugins.py` | 插件管理：列出全部入口、冲突检查、启用/禁用（写 profile 补丁） |
| `dsh-fallback-heal.py` | 补齐 `.dsh-module-fallback` 缺失 junction |
| `dsh_tests.py` | **回归测试（43 用例）**，写操作全在临时副本上 |
| `dsh-selfcheck.py` | 静态自检：语法 / 缺失 import / 未定义名 / GBK 字符 / subprocess 参数名 / bat 行尾 / 解耦（关键词黑名单 + **绝对路径白名单**） |
| `dsh-accept.py` | **一键验收**：静态自检 + 43 回归用例 + bat 语法闸 + 解释器探测 + 行尾检查 |
| `dsh-mutate.py` | **变异测试**：故意改坏 21 处关键逻辑，验证回归用例真能抓到 |
| `dsh-env.py` | 命令行薄壳（`--dump` / `--refresh` / `--json`） |
| `start-dsh.bat` | **启动入口**（复制到桌面用）。薄壳：定位工具链目录 + PATH 钉扎 + 转交 `dsh-launcher.py` |
| `结束-dsh.bat` | **停止脚本**（复制到桌面用）。netstat+taskkill 结束占用 3080 的进程 |
| `MAINTENANCE.md` | **维护与审计说明**（故障速查、审计要点、改动纪律） |

## 改完代码后必做

```bat
python dsh-accept.py         :: 【推荐】一键跑齐下面三段 + 解释器探测 + 行尾检查
```
分步跑也可以：
```bat
python dsh-selfcheck.py      :: 静态自检，0 问题才算完
python dsh_tests.py          :: 43 用例回归，全过才算完
python dsh-env.py --refresh  :: dsh 升级或换目录后强制重探
```

改逻辑**之前**建议先跑 `python dsh-mutate.py`（变异测试）——
它验证"用例是否真的能抓到回归"，这是"全绿"之外的独立一层保证。

> 变异测试**分批前台跑**：`python dsh-mutate.py --range 0:7` /
> `--range 7:14` / `--range 14:21`（汇总行注明本批范围）。
> 整套后台一次跑可能被宿主环境的批删保护钩子误杀（详见
> MAINTENANCE.md「2026-09-30」）。

## dsh 起不来？

按 [MAINTENANCE.md](MAINTENANCE.md) 的**六类故障速查**逐层排除 ——
PATH 抢占 → 包管理器误判 → 链接缺失 → 环境假故障 → 插件自身 bug（crash loop）→ 端口排除范围。
急着进系统时用菜单 **[6] 安全模式**，查完 **[7] 还原**。

## 环境要求

- Windows
- **Python 3.8+**（入口 bat 会自动查找；也可用环境变量 `DSH_PYEXE` 指定）
- Node.js + pnpm（dsh 自身需要）

## 设计约束

- **零硬编码路径**：所有路径从 `__file__` 推导，整个目录可以随意搬动
  （入口 `start-dsh.bat` 同样不写死安装位：见上面「工具链目录怎么找」的三级解析）
- **零 agent 依赖**：不引用、不读取任何 AI 工作台 / IDE 插件的目录或环境变量
  （自检第 8 项会复核这一点）
- **不硬崩**：dsh 破坏性更新后探测不到就降级，并在报告里说明
- **破坏性操作需显式许可**：删链接要 `--clean`，杀进程要 `--kill`

## 运行时缓存（不入库）

以下文件会自动重建，`.gitignore` 已排除：

- `dsh-env.cache.json` —— 上次探测结果
- `dsh-dump.cache.txt` (+ `.meta.json`) —— 权威插件树缓存

它们包含**本机真实路径**，请勿提交到公开仓库。

## 许可

**MIT** —— 见 [LICENSE](LICENSE)。
