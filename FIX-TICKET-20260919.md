# dsh-launcher 安全修复工单

> 来源：`SECURITY-REVIEW-20260919.md`（三镜头验证小组 2/3 裁定，28 项存活）
> 开单日期：2026-09-19　　状态图例：✅ 已完成 ｜ 🔨 待修 ｜ 👀 观察
> **【2026-09-19 完工】** 本单全部 ✅ 项已修完并验证：selfcheck 0 问题、
> 回归 29 用例全绿（新增 6 个守护用例覆盖 T1/T2/F2/F4/F8/T11）、
> 端到端 `--check` 通过（181 入口）。F4 的日志/备份令牌已另行人工清除。
> 完工纪律（每批修完必跑）：`python dsh-selfcheck.py`（0 问题）→ `python dsh_tests.py`（全过）→ 涉判据的修复加变异用例 → MAINTENANCE.md 记变更。

---

## P0 · 立即处置（本次已执行完毕）

### ✅ F3 计划任务 `dsh-e2e-accept`
- **处置**：`schtasks /Delete /TN dsh-e2e-accept /F` → 回执「成功删除」，复核查询已不存在；**dsh 本体（node PID 59560）确认存活**，72h 强杀风险随任务注销消失。
- **代码侧遗留 ✅**：`_e2e_accept.py` 验收流程改为**无论成败都删任务**（成功路径现在 `:123` 明确「不删任务」，失败路径 `:109` 才删）。顺带按 F22 给 `:69-74` 的 taskkill 加 `is_dsh_here` + `pid_is_node` 两道闸。

### ✅ F4 令牌落盘（清理部分）
- **处置**：`_launcher-e2e.log` 内 1 处 `token=` 值已字节级替换为 `<REDACTED>`（其余内容未动）；`~/.dsh/task-board/*.bak-stale-*` 5 份含 token 备份已删除；**活锁 `ledger-v2.lock` 未触碰**（dsh 正在用）；全目录残留扫描 CLEAN。
- **代码侧遗留 ✅（脱敏改造，防复发）**：
  1. `dsh_env.py:435-439` `clean_stale_locks` 备份锁文件前，把 JSON 里的 `token` 字段值替换为指纹（如 `"<redacted:sha1:前8位>"`）再落盘——备份只为排查看结构，不需要真 token；
  2. 自定规矩写入 MAINTENANCE 审计要点：**任何日志/备份/缓存不得出现完整 `?token=` 值**（launcher 现有提示文案只说「复制含 ?token= 的地址」，不落值，保持）；
  3. `_launcher-e2e.log` 等 e2e 产物在脚本收尾时自扫描 `token=[A-Za-z0-9_-]{16,}` 并就地脱敏。

---

## P1 · MEDIUM 代码修复（本周内）

### ✅ T1（F11+F2 同根）启动器跨实例互斥 —— 一个修复消两个 MEDIUM
- **位置**：`dsh-launcher.py:203-252`（守卫与 `subprocess.call` 之间无互斥）；`dsh_env.py:402-442`（清锁扫描窗口）。
- **修法**：
  1. `action_start` 入口创建 `%TEMP%\dsh-launcher.instance.lock`（`os.open(..., O_CREAT|O_EXCL|O_RDWR)`，内容写 pid）；持有至启动流程结束（finally 删除）；已存在时读出对方 pid，`pid_is_node` 无关紧要，直接提示「另一个启动器正在运行（pid N）」并退出；
  2. `clean_stale_locks` 删除每个锁前：`os.stat` 该文件，**mtime 距今 < 5 秒的跳过**（防删正在写入的活锁）；每删 10 个重跑一次 `dsh_running_any_port`，为真立即中止返回。
- **验收**：双开启动器第二实例被拒；手动造 0 字节新锁（mtime 刚刚）+ 无 dsh 环境，清锁跳过它；造 1 小时前的 0 字节锁照常清。变异：去掉互斥/去掉 5 秒窗，新增用例必须变红。

### ✅ T2（F1）`kill_leftover` 击杀护栏
- **位置**：`dsh-launcher.py:111-112`（裸 `subprocess.run(..., timeout=30)`）；顶层 `:502-507` 只捕 KeyboardInterrupt。
- **修法**：包 `try/except Exception` 记 `[!] taskkill 失败：e` 并 continue；击杀后重探 `find_live_ports`，仍有存活则报告后继续（不盲目往下启动）。
- **验收**：变异用例——monkeypatch taskkill 抛 `TimeoutExpired`，断言不崩、日志含「taskkill 失败」、流程走完。

### ✅ T3（F10）长操作反馈 + 探测早退
- **位置**：`dsh_env.py:367-384`（`ex.map` 在 `with` 内 return → 等全部 future）；`:737-738`（90s）、`:786/:827-829`（240s）；`dsh-launcher.py:265`。
- **修法**：
  1. `dsh_running_any_port` 改手动 `submit` + 消费 `as_completed`，首个命中即 `shutdown(wait=False, cancel_futures=True)` 后返回；
  2. `_probe_http` 回环超时 3s → 1s；`is_dsh_here` 连接超时保持 1.2s；
  3. `action_env` / `clean_stale_locks` / plugins `r` 刷新前各打一行「正在探测…（最坏约 N 秒）」。
- **验收**：24 个假监听端口语境下（可 mock `node_listening_ports`），首个端口命中时函数返回耗时 ≪ 全量收尾耗时；菜单 [5] force 路径首屏必有一行进度提示。

---

## P2 · LOW 修复（顺手批次，每条 ≤ 半小时）

| # | 位置 | 修法 | 验收 |
|---|---|---|---|
| ✅ T4 (F5) | `dsh-launcher.py:371`；`dsh-plugins.py:649`；`dsh_env.py:104,470,594,694,1046-1048` | action_env 包 try；刷新 collect 失败保留旧 `st` 并提示；`load_config` 对 start_command/port_candidates 加 isinstance；`_score_repo`/`find_profile` 对 pj 加 isinstance dict；report() 改 .get | config 写 `"start_command": 123`、`"port_candidates": "x"`、package.json 改成数组 → 菜单 [5] 与插件 r 不崩，有可读提示 |
| ✅ T5 (F6) | `dsh_env.py:944-957, 971-984` | `_cache_valid` 首行 `if not isinstance(c, dict): return False`；命中路径 `r` 非 dict 时落回全量探测 | 缓存手改成 `[1]`/`5` → 菜单 [1] 正常全量探测，不误报「没仓库」 |
| ✅ T6 (F7) | `dsh_env.py:728-729, 675-705` | start_command 首段解析为存在的文件 → 直接 `[该文件] + 其余参数` 启动（绕过 cmd）；否则 `["cmd.exe","/s","/c", '"'+整体+'"']` 显式外引号 | `"C:\Program Files\x\x.cmd" dsh web --title "my h"` 能起；7 组 splitter 用例全过 + 新增 4-quote 用例 |
| ✅ T7 (F8) | `dsh-plugins.py:401, 546, 619`；写前校验 | eid 不匹配 `ID_RE` 字符集 → 拒写并提示（不追加）；render/list 打印 id 前 `re.sub(r'[\x00-\x1f\x7f]', '?', s)` | 造 `*x`/含 ESC 的假 dump → set_disabled 拒绝；render 输出无控制符 |
| ✅ T8 (F9) | `dsh-selfcheck.py:235-256` | COND_RE 加 `re.I`；去缩进门槛；`exist\s+("[^"]*"|\S+)`；补 `\S+\s*==\s*\S+`；剥引号段与 rem 行后再计括号 | 4 种漏检形态的 bat 样本各造一版 → 自检全部报「高」；现役 start-dsh.bat 仍 0 问题 |
| ✅ T9 (F12) | `dsh_env.py:835-841` | DUMPC 与 meta 均 tmp + `os.replace`（meta 后写）；meta 增加字节数校验，读时不符即弃缓存 | 中断注入（写一半抛异常）→ 下次读缓存拒收并重跑 dump |
| ✅ T10 (F13) | `dsh-launcher.py:99-116`；`dsh-fallback-heal.py:100-112` | 击杀循环前 `netstat_table(refresh=True)`；逐 PID 复核其仍监听目标端口后才 taskkill | mock 两个快照（PID 换手）→ 旧 PID 不被杀 |
| ✅ T11 (F14) | `dsh_env.py:693-705` | 跟踪反斜杠串（按 MSVCRT 规则）；独立 `""` 产出空参数；仅 ASCII 空格/制表切分；空白输入返回 `[""]` 并由调用方拒收 | 新增 6 组边界用例（`""`、`a""b`、`"a\"`、NBSP、空白串） |
| ✅ T12 (F15) | `dsh_env.py:827-829, 753-754, 775` | profile_name 与 prebuilt 路径进 argv 前过 `cmd_arg_safe` 同款字符集校验，不过则跳过该候选并记 note | 造 `x&calc` 目录名的假 profile → 不出现在启动候选 / dump 拒绝并留 note |
| ✅ T13 (F16) | `dsh-fallback-heal.py:32, 166-189` | `(link+target).encode("gbk")` 不干净的条目直接走参数列表回退；`_CMD_UNSAFE` 扩为 `[\x00-\x1f\x7f%&|<>^!"]` | 造非 GBK 目录名 → 链接仍建成（回退路径）；0x1A 名被拒 |
| ✅ T14 (F17) | `dsh_env.py:72-73, 43, 859` | `_run` 解码改 UTF-8 优先 GBK 兜底（try 链）；`_re_id` 捕获含空白的 id 时告警并原样保留；分节头判定放宽为 `# ==` 或 `#===` | 造含中文 id 的 dump → 解析/写回往返一致；`- id: a b` 不再整行丢失 |
| ✅ T15 (F18) | `dsh-selfcheck.py:28-29, 279, 292` | PY_FILES 追加目录内现存 `_*.py`（动态 glob）；.py 剥注释改 `tokenize`；bat 剥 `rem` 改词边界匹配 | 现状即红：`_accept_all.py:15` 的 `.workbuddy` 路径必须被报出（先修 T15 自己，再决定该临时脚本删改） |
| ✅ T16 (F19) | `dsh-plugins.py:689, 695` | `cmd.isdecimal()` 替代 isdigit，或 `int()` 包 try | 输入 `²` → 提示无效输入，UI 存活 |
| ✅ T17 (F20) | `dsh-fallback-heal.py:152-154, 171-178, 224-226` | communicate 超时后 `proc.kill()` + 二次 communicate；两处 rmdir 包 try | mock 超时 → 子进程被杀、脚本继续跑完不崩 |
| ✅ T18 (F21) | `dsh_env.py:435-454`；`dsh-plugins.py:359-374` | 备份名加 `%f` 毫秒 + pid：`.bak-stale-%Y%m%d-%H%M%S-%f`；轮转改按前缀 `glob(fp + ".bak-stale-*")`；两处 keep 值统一并写入文档 | 同秒两次清理产生两份备份互不覆盖；双锁目录各留各的 5 份 |
| ✅ T19 (F23) | `dsh-fallback-heal.py:207, 222-229` | 删除前确认目录内含 `package.json` 或为 junction（真包形态校验），否则只列不删 | 造 `foo_tmp_12_ab` 空目录 + `--clean` → 只列不删 |
| ✅ T20 (F24) | `dsh-env.py`、`dsh_tests.py` | 两文件补 `sys.stdout.reconfigure(errors="replace")`（与其余五个文件一致） | PYTHONIOENCODING 强制 936 下跑 --json 含中文路径不崩 |

---

## P3 · INFO 观察（不强制，择机）

- ✅ F25：`dsh_tests.py` 真实用户名路径已改中性假路径（demo-user）。
- 👀 F26：`start-dsh.bat:2` delayedexpansion 的 `!` 陷阱 + `chcp 936` 硬编码 —— 本机无碍，迁移机器时再处理。
- 👀 F28：`dsh-env.cache.json` 非原子写 —— fail-safe，**保持现状**（勿与 F12 混同）。
- 👀 F29：Ctrl+C 在 check/env 期间退出整个启动器 —— 可改为回菜单，UX 取舍。
- 👀 F27 备注：将来若新增 `_*.bat`/`_*.json` 临时文件，给 .gitignore 补对应模式。

---

## 批次建议与回归要求

1. **第一批（P1）**：T1-T3 + F4 代码脱敏。改完必跑：selfcheck 0 问题、17 用例全过、**T1/T2 各配 1 个变异用例**（判据改动必须能被测试抓住——项目血泪规则）。
2. **第二批（P2 快速批）**：T4/T5/T16/T20（半小时级）→ 自检+回归。
3. **第三批（P2 解析器批）**：T6/T7/T8/T11/T13/T14（都动解析/生成，务必配正反用例 + 真实 dump 缓存比对）。
4. **第四批（P2 其余）**：T9/T10/T12/T15/T17/T18/T19。
5. 每批完成更新本工单状态与 MAINTENANCE 变更记录；全部完成后跑一次 `dsh-env.py --refresh` 重建缓存。

**红线提醒**（改动时不许破）：击杀必过 `pid_is_node`；清锁三重把关不许放宽；strict 签名 `401 + authentication required + dsh web` 不许放宽；写补丁必有备份 + 原子替换 + 入口数校验。
