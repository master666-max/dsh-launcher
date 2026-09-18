# dsh 启动器 · 维护与审计说明

> 最后更新：2026-09-18　　维护人：（本机）
> 独立目录：`%USERPROFILE%\dsh-launcher`（与 `deepseek-harness` 同级）

---

## 一、这是什么

一套**完全独立**的 dsh（DeepSeek Harness）启动与运维工具链。

- **不依赖 WorkBuddy**：目录、代码、运行时都与 WorkBuddy 无关；
  WorkBuddy 被卸载后本套工具照常工作。
- **不依赖 dsh 源码的内部结构**：仓库路径、profile、端口、启动命令全部运行时探测，
  dsh 破坏性更新后仍能自己找到路；找不到就降级并在报告里说明，绝不硬崩。
- **入口唯一**：桌面 `start-dsh.bat`。双击 → 1 秒内不做任何输入 → 自动启动 dsh；
  1 秒内按任意键 → 进入菜单。

## 二、文件清单与职责

| 文件 | 职责 | 谁会 import 谁 |
|---|---|---|
| `dsh_env.py` | **探测层（单例，唯一认识 dsh 的模块）**：仓库定位、profile 定位、端口探活、启动命令探测、权威插件树、进程/锁管理、脏锁清理 | 被下面三个模块 import |
| `dsh-launcher.py` | 编排 + 交互菜单：启动 dsh、调自愈、调插件工具。**只做编排，不含 dsh 内部知识** | import dsh_env |
| `dsh-plugins.py` | 插件管理：列出 181 个入口、冲突检查、启用/禁用（写 profile 补丁） | import dsh_env |
| `dsh-fallback-heal.py` | 补齐 `.dsh-module-fallback` 缺失 junction（可子进程或直跑） | import dsh_env |
| `dsh_tests.py` | **回归测试 16 用例**（改完代码必跑） | import dsh_env、加载 dsh-launcher/plugins |
| `dsh-selfcheck.py` | 静态自检：语法/缺失 import/未定义名/GBK 字符/subprocess 参数名/bat 行尾 | 无依赖 |
| `dsh-env.py` | 命令行薄壳（`--dump` / `--refresh` / `--json`），实现都在 dsh_env.py | import dsh_env |

数据/缓存（可随时删除，会自动重建）：
`dsh-env.cache.json`（探测缓存）、`dsh-dump.cache.txt` + `.meta.json`（权威插件树缓存）。



### 配套的「停止 dsh」脚本
桌面还有 ：用 netstat+taskkill 结束占用 3080 的进程。
它**不依赖任何目录**（只用 netstat/taskkill），与本套工具无路径耦合。
注意：强杀会留下脏锁，启动器已在下次启动时自动清理。

## 三、日常操作

### 启动
双击 `%USERPROFILE%\Desktop\start-dsh.bat`。
1 秒内不做任何输入 → 自动启动；按任意键 → 进菜单（[1]启动 [2]插件 [3]查冲突 [4]补链接 [5]环境报告 [0]退出）。

### 改完代码后必做的三件事
```
python dsh-selfcheck.py     # 静态自检（0 问题才算完）
python dsh_tests.py         # 16 用例回归（全过才算完）
python dsh-env.py --refresh # 强制重探（dsh 升级/换目录后）
```

### 改动纪律（血泪规则）
1. **每处改动必须有"改动前/后"的实测证据**，否则不许说"修好了"。
2. **删/加 import 后必跑 dsh-selfcheck.py** —— socket / glob / shutil 三次都是它抓的。
3. **涉及强杀（taskkill /F）的测试，结束必须做状态清理**（会留下 dead-PID 锁）。
4. **测试必须写成文件跑**：inline `bash -c` 会把 Windows 路径 `\` 变 `/`，得出错误结论。
5. **所有写操作测试都在 tempfile 副本上做**，绝不碰真实补丁。
6. **改代码一律用编辑器工具，不要在 shell 里内联拼 `\n`** —— shell 会把 `\n` 吃掉变成 `/n`，补丁脚本会静默写坏文件（踩过三次）。
7. **本工具与任何 AI agent / IDE 插件完全解耦**，自检第 8 项会复核；不要为了图方便往代码里加 agent 路径。

## 四、审计要点（这工具链有哪些"会咬人"的地方）

### 1. task-board 脏锁（最阴）
`~/.dsh/task-board/ledger-v2.lock` —— `@linxin666/dsh-client-ui-task-board` 的锁。
**强杀 dsh 会留下三种形态的锁**，任何一种都会让 dsh 下次启动直接崩
（`plugin tree failed to load ... lock is unreadable`）：
① 0 字节　② 内容非合法 JSON　③ **格式合法但记录的 pid 已死**（最常被漏掉）。

处置：启动器已在启动前自动清理（`clean_stale_locks`），三重把关缺一不可：
候选端口无 dsh → **任意 node 端口也无 dsh** → `lock_is_stale` 判脏 → 备份后删。
**审计时若发现有人放宽这些前提，立刻回滚。**

### 2. 清理残留实例只认端口+签名
绝不按镜像名杀 `node.exe`（会误杀用户其它工具链）。
只对「HTTP 401 + 正文含 `authentication required` + 含 `dsh web`」的端口动手。

### 3. 启动前不补 fallback 链接
dsh 每次只建它需要的 220/285 个链接，多补的会被它删掉，
**实测全补齐反而慢 4 秒**。菜单 [4] 是手动入口（deep=True）。

### 4. 预编译入口 `apps/cli/lib/bin.js` 只作最后兜底
它比 tsx 现编译快约 6 秒，但走的是非官方代码路径。
且有**过期检测**：`apps/cli/src` 下任何 `.ts` 比 `lib/bin.js` 新就回退 tsx。
**审计时确认这条过期检测还在，否则会跑旧代码。**

### 5. 端口识别靠签名不靠端口号
`is_dsh_here` = HTTP 401 + `authentication required` + `dsh web`。
**绝不能放宽成"正文含 dsh"** —— 那会误杀任何正文带 dsh 字样的本地服务
（实测：写 "this is NOT dsh" 的假服务都会被判成 dsh）。

### 6. dsh 是用「cwd=仓库 + 相对路径」启动的
**判断 dsh 是否在跑不能靠进程命令行里的仓库路径** —— 命令行里没有。
正确做法：`netstat` + `tasklist` 找 node.exe 监听端口再探签名
（实测能抓到 dsh-mobile 的 3443，光看 3080 会漏）。

## 五、与 AI Agent / IDE 插件的解耦声明（已做到【完全】解耦）

| 检查项 | 结果 |
|---|---|
| 代码目录 | `%USERPROFILE%\dsh-launcher`，不在任何 agent 目录内 ✅ |
| 7 个 .py 文件里的 agent 目录引用 | **0 处** ✅ |
| 代码读取 agent 环境变量 | **0 处** ✅ |
| 桌面入口 `start-dsh.bat` 里的 agent 引用 | **0 处** ✅ |
| 运行时解释器 | 系统 Python（`%LOCALAPPDATA%\Programs\Python\...`）✅ |
| 写出的文件 | 只写自己的目录 + `~/.dsh`（dsh 状态目录）✅ |
| 对其它 agent 的影响 | 无依赖、无调用、无共享文件 ✅ |

**解释器查找顺序**（入口 bat 内，全部与 agent 无关）：
1. 环境变量 `DSH_PYEXE`（想指定就设它）
2. 用户级安装：`%LOCALAPPDATA%\Programs\Python\Python{314..310}\python.exe`
3. 机器级安装：`%ProgramFiles%\Python{314..310}\python.exe`
4. Windows `py -3` 启动器
5. `PATH` 里的 `python`（自动排除 WindowsApps 商店桩）

**自检已内置解耦检查**：`dsh-selfcheck.py` 每次都会扫描 6 个代码文件与入口 bat，
一旦出现 `.workbuddy` / `codebuddy` / `zcode` / `aicoding` 之类的引用会直接报「很高」。
**换句话说：解耦不是一次性动作，而是每次自检都会复核的常驻约束。**

> 若本工具需要迁移到别的机器/别的用户名：整个 `dsh-launcher` 文件夹拷过去即可，
> 代码内的路径全部从 `__file__` 推导，不含任何用户名硬编码。
> 只需把桌面 `start-dsh.bat` 里的 `%USERPROFILE%\dsh-launcher` 保持默认即可。

## 六、常见故障速查

| 症状 | 原因 | 处置 |
|---|---|---|
| `plugin tree failed to load ... lock is unreadable` | task-board 脏锁 | 启动器已自动清；手动可删 `~/.dsh/task-board/*.lock`（确认无 dsh 在跑） |
| `EPERM: symlink` | fallback 竞态 | 菜单 [4] 手动补齐 |
| 找不到 pnpm / 不是内部命令 | PATH 被裁剪 | bat 已做 PATH 钉扎；确认 `%APPDATA%\npm` 在 PATH |
| 插件状态显示旧数据 | dump 缓存 | 菜单 [5] 或 `python dsh-env.py --refresh` |
| 黑框闪一下就退 | .bat 行尾不是纯 CRLF | 用 `dsh-selfcheck.py` 查 |

## 七、变更记录

- **2026-09-17**：建立（launch 经历三轮提速 105s→43s→15s）；修 13+ 轮 bug。
- **2026-09-18**：
  - 全面重构：探测层单例化（`dsh_env.py`）、netstat 单次快照、
    重复实现下沉、回退判据改端口探活、缓存深拷贝、--dump 守卫。
  - 新增 `dsh_tests.py`（16 用例）与 `dsh-selfcheck.py`（7 类静态检查）。
  - 查明并修复「task-board dead-PID 锁导致 dsh 起来就退出码 1」。
  - **整体迁出 WorkBuddy 目录** → `%USERPROFILE%\dsh-launcher`（本次）。

## 八、推送到 GitHub（含本机特有的坑）

远程私有仓库：**https://github.com/master666-max/dsh-launcher**

### 常规更新流程
```bat
cd %USERPROFILE%\dsh-launcher
git add -A
git commit -m "说明"
git push
```

### 本机两个必知前提

**① push 会被全局规则改写成 SSH（22 端口被拒）**

本机 git 全局配了：
```
url.git@github.com:.pushinsteadof = https://github.com/
```
于是 `git push` 会被改写成 `git@github.com:...`，而 SSH 22 端口在本机连不上。
推之前先临时禁用这条规则、推完恢复：

```bat
git config --global --unset url.git@github.com:.pushinsteadof
git push
git config --global url.git@github.com:.pushinsteadof https://github.com/
```

**② 命令行里不要带沙箱代理变量**

若 shell 里有 `http_proxy=http://127.0.0.1:7207`，GitHub 会报
`CONNECT tunnel failed, response 502`。推送前清掉代理即可（加速器是 hosts 改法，
DNS 指到 127.0.0.1，直连反而通）：

```bat
set http_proxy=
set https_proxy=
set HTTP_PROXY=
set HTTPS_PROXY=
```

### 绝不入库的内容
`.gitignore` 已排除：`dsh-env.cache.json`、`dsh-dump.cache.txt`(+meta)、
`__pycache__`、`*.bak-*`、`_*.py`。**缓存文件含本机真实路径与个人目录结构，永远不要提交。**

### 提交前自检
```bat
python dsh-selfcheck.py      :: 含解耦检查
python dsh_tests.py          :: 16 用例
```
