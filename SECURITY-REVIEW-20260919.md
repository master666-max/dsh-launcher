# dsh-launcher 安全审查报告

**审查对象**：`%USERPROFILE%\dsh-launcher` @ git `1b3cd83`（工作区干净），含桌面入口 `start-dsh.bat`。
**时间 / 方式**：2026-09-19，纯静态审查 + 只读现场核验（未执行项目代码、未运行任何利用）。
**方法**：复刻 claude-security 插件管线形态——3 名研究员分镜扫描（注入面 / 运行稳定性 / 竞态+secrets）→ 3 镜头验证小组对抗式投票（怀疑论、代码溯源、汇总裁定），2/3 通过才保留。
**结论**：候选 29 项 → 存活 28 项。**无 CRITICAL/HIGH**；MEDIUM 5 项、LOW 19 项、INFO 4 项；另击杀 1 项（.gitignore 理论缺口，无现实对象）。2 项需要**立即处置**（见 M1/M2）。

> 声明：本报告是 claude-security 方法论的人工复刻，**不是**该插件管线产物，无其 `verification.status` 印章；每项发现的 Verification 标注的是本次三镜头投票结果。所有发现均来自阅读与只读核验，没有任何利用被实际触发。

---

## Coverage（覆盖范围）

**已逐行审读**：7 个跟踪 .py（dsh_env 1091 行、dsh-launcher 507、dsh-plugins 744、dsh-fallback-heal 297、dsh-selfcheck 321、dsh_tests 723、dsh-env 30）、桌面 `start-dsh.bat`（含字节级行尾检查）、`.gitignore`、两份文档。
**已静态审读**：全部 7 个 `_*.py` 临时脚本与 `_*.out`/`_launcher-e2e.log` 日志（只读）。
**已现场核验**：`dsh-env.cache.json`（结构良好）、`dsh-dump.cache.txt`（181 个 id 全 ASCII）及其 meta（**当前在磁盘上缺失**）、`schtasks` 活动任务、`~/.dsh/task-board` 锁与 5 份备份内容、全部 17 个未跟踪文件的 `git check-ignore` 状态（均被忽略，`git add -A` 不会带入任何敏感物）。
**刻意不查**：dsh/node 自身内部、`__pycache__`、`.git` 内部、`~/.dsh` 其余文件（`.credentials.yaml` 不在任何代码路径上）、deepseek-harness 仓库本体。
**方法局限**：所有竞态窗口宽度为推理值，未做运行时测量（静态审查不允许执行）。

---

## MEDIUM 发现（5 项）

### F2 — 清脏锁 TOCTOU：正在写入的活锁可被误删（MEDIUM，confidence high）
**影响**。正在启动的 dsh 丢失锁文件 → 正是本工具链要根治的「锁不可读崩溃」家族；还可能造成双实例。
**位置**。`dsh_env.py:402-442`（0 字节判脏 `:351-352`，备份+删除 `:437-438`）。
**机理**。守卫（netstat ok + 全端口无 dsh）是瞬时快照；随后的逐文件扫描宽达数秒，每个带 pid 的锁还要跑一次 tasklist（最长 25 秒）。此间隙内并发启动的 dsh 正在写锁——0 字节或半截 JSON 恰好命中判脏条件；死-pid 检查只保护「已写完整」的锁。node/libvfs 以 FILE_SHARE_DELETE 打开文件，`os.remove` 能删掉被持有中的锁。
**场景**。双击启动器两次（第一次因 F10 的静默探测显得没反应）→ 实例 A 过守卫开始扫描 → 实例 B 的 dsh 写第一个锁 → A 判 0 字节脏 → 备份后删除 → B 的 dsh 无锁运行或崩溃。
**前提**。并发启动与扫描窗口重叠（现实触发路径 = F11）。
**修复**。删除前重校验：文件 mtime 年轻于 ~5 秒即跳过；或每删一个锁前重跑 `dsh_running_any_port`；或给 action_start 加跨实例互斥（与 F11 同一修复）。
**Verification**。3/3 镜头确认（怀疑论镜头实测 FILE_SHARE_DELETE 语义后维持）。

### F3 — 遗留计划任务 `dsh-e2e-accept`：今晚自动拉起 dsh，且已卡「正在运行」（MEDIUM，confidence high，活发现）
**影响**。(a) 今晚 23:59 计划任务会无人工介入自动启动 dsh + 含 token 的 URL；(b) 该任务自 01:15 起卡在「正在运行」，进程树里挂着真实 dsh（node.exe PID 59560，现占 3080）——schtasks 默认 72 小时强制停止，约 9/22 01:15 任务计划程序会**强杀整棵树**，再留一套脏锁。
**位置**。`_e2e_accept.py:77-78` 创建（`/sc once /st 23:59`）；成功路径**不删任务**（`:123` 注释「不删任务」；失败路径 `:109` 才删）。现场 `schtasks /query` 已核实：已启用 / 正在运行 / 上次运行 2026-09-19 01:15:43 / 下次 23:59 / 72h 限制。
**场景**。9/22 01:15 用户 dsh 正在用时被系统无征兆杀掉，锁全脏，下次启动依赖 F2 的清理路径。
**前提**。无——已在机器上。
**修复**。立即 `schtasks /delete /tn dsh-e2e-accept /f`（先决定 dsh 去留）；临时脚本的验收流程改为结束必删任务。
**Verification**。3/3（两名验证员均独立现场核实任务状态）。

### F10 — 多分钟静默阻塞；线程池提前退出被击穿（MEDIUM，confidence high）
**影响**。菜单 [5] 强制路径最坏 **90s（pnpm --help）+ 240s（dump-config）串行 ≈ 5.5 分钟**无任何输出；每次菜单 [1] 的 `clean_stale_locks` 里 `dsh_running_any_port` 即使第一个端口命中也要等全部探测收尾（每端口最坏 4.2 秒，8 并发，24 个 node 端口 ≈ 13 秒静默）；每个候选命令尝试后 `live_now` 再串行探 ≤8 端口。用户感知「窗口死了」→ 双击（喂 F11）或强关（喂 F2 的半完成态）。
**位置**。`dsh_env.py:367-384`（`ex.map` 预提交全部任务，`return True` 在 `with` 块内 → `__exit__` 等待所有 future）；`:737-738`、`:786/:827-829` 超时值；`dsh-launcher.py:265`。
**场景**。菜单 [5] 后屏幕冻结 5 分钟，用户认为死机。
**前提**。无（缓存命中时瞬时；force 路径必然长等）。
**修复**。(a) 长操作前打印一行进度提示（含最坏时长）；(b) `dsh_running_any_port` 改手动 submit + 首中即 `shutdown(wait=False, cancel_futures=True)`；(c) 回环探测 HTTP 超时 3s→1s。
**Verification**。3/3（怀疑论镜头指出「4.2s 仅对活监听端口成立」，不影响结论）。

### F11 — 双实例竞态：两个启动器都过「未运行」守卫，双双拉起 dsh（MEDIUM，confidence high）
**影响**。第二个 dsh 死于端口/绑定，或与第一个一起踩 dsh 自身的 junction 并发竞态（EPERM/EEXIST 崩溃家族——`dsh-fallback-heal.py:5-11` 记录的原始事故）；还会触发 F2。
**位置**。`dsh-launcher.py:203-252`（守卫 `find_live_ports` → 数步之后才 `subprocess.call`；全程无跨进程互斥）。netstat 缓存是进程内的（`dsh_env.py:163-196`），互相看不见对方的在途启动。
**场景**。见 F2/F10；单用户双击是最易触发的竞态。
**前提**。两实例时间窗重叠。
**修复**。`action_start` 全程持有跨实例标记（`%TEMP%` 下 `O_CREAT|O_EXCL` 独占文件 + pid，或 `msvcrt.locking`）；启动前一刻复查 `find_live_ports`。
**Verification**。3/3（面板指出 F2+F11 同根——互斥修复同时消掉两者的大部分可达面）。

### F1 — `kill_leftover` 的 taskkill 无异常护栏：TimeoutExpired 击穿顶层守卫（MEDIUM，confidence medium）
**影响**。taskkill 挂起 ≥30 秒（WMI/RPC 停滞）→ `TimeoutExpired` 一路上抛；`__main__` 顶层只捕 `KeyboardInterrupt`（`dsh-launcher.py:502-507`）→ traceback、窗口秒退、击杀半完成、dsh 状态未知。
**位置**。`dsh-launcher.py:111-112`（裸 `subprocess.run`，无 try）；姊妹路径 heal 脚本的同类击杀**有**护栏（`dsh-fallback-heal.py:102-114`）——证明护栏是本意、此处遗漏。
**场景**。dsh 假死占 3080，用户选 [1] 确认清理，系统繁忙 taskkill 卡 30 秒 → 崩。
**前提**。tasklist 先成功（否则到不了 taskkill）+ taskkill 挂 ≥30s（少见）。
**修复**。包一层 `try/except Exception` 记日志；或改走永不抛的 `dsh_env._run()`；击杀结果不确定时重探 `find_live_ports` 再继续。
**Verification**。3/3（怀疑论镜头降级 HIGH→LOW 的理由被裁定采纳为「条件叠得多」，最终按稳定性优先定 MEDIUM）。

---

## LOW 发现（19 项）

**F4 — 凭据落盘（两处）**（LOW，3/3）。`_launcher-e2e.log:29` 含真实 dsh web `?token=` URL（gitignored，但随仓库目录生活）；`~/.dsh/task-board/*.bak-stale-*` 5 份备份（`dsh_env.py:437` 逐字节复制）内含会话 token。单用户本机、gitignore 生效，故 LOW；但任何「顺手拷 ~/.dsh」的备份习惯都会带走可重放样式的令牌。修复：日志脱敏（只记端口不记 token）；锁备份把 token 字段替换为指纹。**secrets pass 全量结果**：其余 24 个文件无凭据值命中（dump 缓存里的 `apiKeyEnv: DEEPSEEK_API_KEY` 等只是环境变量**名**，无值）。

**F5 — 菜单 [5] 与插件 r 刷新无异常护栏 + 类型盲区**（LOW，3/3）。`dsh-launcher.py:371` 裸调 `detect(force=True)`（绕过了 `probe()` 的 try）；`dsh-plugins.py:649` 的 `collect(force=True)` 在 `:577-581` 的 try 之外。类型洞：`start_command` 非字符串 → `split_win_cmdline` `for ch in s` TypeError（`dsh_env.py:694`）；`port_candidates` 非列表 → `:104` TypeError；package.json 合法 JSON 但非对象 → `pj.get` AttributeError（`:470`、`:594`）；`report()` 直接下标 `:1046/:1048`。修复：两处补 try（失败保留旧状态）；`load_config`/`_score_repo`/`find_profile` 加 isinstance 检查。

**F6 — `_cache_valid` 类型盲（仅外部手改缓存可触发）**（LOW，2/3，怀疑论镜头击杀后被溯源镜头与裁定否决击杀）。`dsh_env.py:944-957` 对「合法 JSON 但顶层非对象」的缓存抛 AttributeError；经 `probe()` 吞掉后菜单 [1] 误报「没定位到仓库」并阻断启动。内部写入方只产 `{"result": dict}`，唯一向量是外部编辑/损坏成合法异形 JSON。修复一行：`_cache_valid` 首行 `if not isinstance(c, dict): return False`（命中路径 `:971-984` 同样处理）。

**F7 — `cmd /c` 外层引号规则：双重引号的 start_command 必然启动失败**（LOW，3/3）。`dsh_env.py:728-729` 把 `split_win_cmdline` 结果拼给 `cmd.exe /c`；「带引号程序路径 + 带引号参数」产生 ≥4 个引号 → cmd 剥首尾引号 → 命令名被截断。`split_win_cmdline`（`:675-705`）只镜像 argv 规则、不管 cmd /c 规则。当前 config_file 不存在故未触发（潜伏）。修复：首 token 解析为真实文件时绕过 cmd 直接以列表参数启动；否则用 `/s ` 显式外引号包裹。

**F8 — dump 派生入口 id 直写 YAML + 终端转义注入**（LOW，3/3）。`dsh-plugins.py:401` 裸写 `- id: %s`；id 来源 `_re_id` `[^\s]+`（`dsh_env.py:43`）可含 YAML 指示符（`|` 块标量会吞掉下一行的 disabled）与 ESC 控制符（render `:546`、--list `:619` 原样打印 → UI 欺骗）。真实 dump 181 个 id 全部落在安全字符集内，需恶意/异型插件 id 才触发（已接受的 npm 信任层）。次生：charset 外 id 永远匹配不到（`ID_RE` `:72`），每次切换都走追加路径堆重复块且入口数校验拦不住。修复：写入前按 ID_RE 字符集校验 eid（拒绝或加引号转义）；打印前剥离 C0 控制符。

**F9 — selfcheck 的 bat 跨行 if 回归器存在系统性漏检**（LOW，3/3）。`dsh-selfcheck.py:235-256`：小写限定（`IF NOT` 漏）、要求缩进（顶格 if 漏）、`exist\s+\S+` 止于空格（带引号空格路径漏）、括号深度计数把 echo/rem 里的括号算进去（`echo :)` 全文件破坏深度）。这台机器秒退事故的常驻守卫可以被上述任何形态绕过。修复：`re.I`；去缩进门槛；`exist\s+("[^"]*"|\S+)`；剥离引号段与 rem 后再计括号。

**F12 — dump 缓存非原子写 + 旧 meta 可为截断树背书**（LOW，3/3）。`dsh_env.py:836-839` 先截断写 DUMPC 后写 meta；读路径 `:818-824` 只信 `meta.fp` + 文件存在。崩溃/双实例交错 → 截断的「权威树」被旧 meta 放行，插件列表静默失真。现场：meta 当前缺失、dump 在——**这对文件已经失同步过一次**。修复：照搬 `set_disabled` 的 tmp + `os.replace`（meta 后写）；meta 里加长度/哈希校验。（同根的 `dsh-env.cache.json` 非原子写是 fail-safe 的：截断 JSON → 全量重探，可接受。）

**F13 — 击杀链消费 ≤3 秒陈旧 netstat 快照；全仓库无一处 `refresh=True`**（LOW，3/3，属已知 pid_is_node 残余窗口的新角度）。PID 归属端口从不复核：陈旧 PID 在 TTL 窗口内被复用给另一 node.exe 时，`pid_is_node` 反而放行。修复：击杀路径先 `netstat_table(refresh=True)`，并对每个 PID 复核其仍监听目标端口。

**F14 — `split_win_cmdline` 与 Windows 规则的四处偏差**（LOW，3/3）。空参数消失（`if cur:`）、双引号折叠、反斜杠-引号串不按 MSVCRT 规则、`ch.isspace()` 按 Unicode 空白切分、空白输入返回 `[s]`。入参是用户自己的 config（信任层内），属健壮性。修复：跟踪反斜杠串、空 `""` 产出空参数、仅按 ASCII 空格/制表切分；测试补 `""`/`""`内嵌/反斜杠引号用例。

**F15 — profile/仓库目录名无元字符闸直进 cmd.exe 参数**（LOW，3/3）。`dsh_env.py:827-829/:753-754/:775`：名为 `x&calc` 的 profile 目录（`os.listdir` 原样取用）拼出 `--profile x&calc`。需要本地写 `~/.dsh/profiles` 的权限（与「仓库脚本本来就会执行」同一信任层）。修复：heal 里现成的 `cmd_arg_safe`/白名单应用到这两处来源。

**F16 — mklink stdin 脚本 GBK 折叠只造成「少补」不造成「错补」；`_CMD_UNSAFE` 漏其余 C0**（LOW，3/3）。非 GBK 名折叠成 `?` → mklink 链接侧先报无效名失败；fast path 的逐条失败**不会**触发 Unicode 参数回退（回退只在 communicate 抛异常时走，`:171-189` 已核实）→ 该条永远缺。0x1A（Ctrl-Z）能静默终止 cmd 的 stdin 读取。修复：`p.encode("gbk")` 不干净的条目直接走参数列表回退；`_CMD_UNSAFE` 扩到全部 C0。

**F17 — GBK 解码使非 ASCII id 变乱码 → 切换报 [OK] 实为空操作**（LOW，3/3）。`dsh_env.py:72-73` 按 GBK 解 node 的 UTF-8 输出；`_re_id` 的 `\s*$` 使 `- id: a b` 整行落榜；`#===`（无空格）分节头不被识别（`:859`）。当前 dump 全 ASCII。修复：子进程输出按 UTF-8 优先、GBK 兜底；id 捕获含空白时告警。

**F18 — 解耦守卫的盲区是真实的：`_accept_all.py:15` 硬编码 `.workbuddy` 解释器路径**（LOW，3/3）。`PY_FILES`（`selfcheck.py:28-29`）不含 `_*.py`，`split("#")`/`split("rem")` 剥注释可被绕过——工具链现磁盘上就躺着一个解耦违规样本而自检全绿。修复：自检加扫 `_*.py`（至少报硬编码绝对解释器路径）；注释剥离按语法处理。

**F19 — `'²'.isdigit()` 为 True 而 `int('²')` 抛 ValueError → 插件 UI 闪退**（LOW，3/3，Python 语义已实测）。`dsh-plugins.py:689/695`。修复：`isdecimal()` 或 try 包 int。

**F20 — 自愈链半完成态：cmd.exe 孙进程泄漏 + rmdir 超时无护栏**（LOW，3/3）。`Popen.communicate(timeout=600)` 抛出后子进程**不被杀**（全仓库无 kill/terminate，已 grep），已喂入的 mklink 脚本继续执行——launcher 打印「已放弃」后 junction 仍陆续出现，用户立即启动 dsh 就撞上自愈竞态窗口；`:152-154`、`:224-226` 的 rmdir `timeout=120` 无 try。修复：超时后 `proc.kill()` + 二次 communicate；rmdir 包 try。

**F21 — 备份轮转按目录不按锁；1 秒时间戳可互相覆盖**（LOW，3/3）。`dsh_env.py:445-454` 对整目录 `sorted(glob)[..-5]`——同目录两把锁时可能清掉 A 的唯一备份而保留 B 的 5 份；同秒两次清理 `copy2` 静默覆盖（`backup_patch` keep=10 vs 锁 keep=5 也欠说明）。修复：备份名带锁名+ms/pid；按前缀轮转。

**F22 — `_e2e_accept.py` 无验证击杀 3080/3443 占用者**（LOW，3/3）。`:69-74` 直接 taskkill，无 `is_dsh_here`/`pid_is_node`——违反项目自己的击杀铁律（MAINTENANCE 四·2/四·7）。dev 脚本但常驻磁盘且被文档当作可重跑验收工具。修复：复用 dsh_env 的两道闸。

**F23 — `clean_pnpm_leftovers` 仅凭名字正则递归删除**（LOW，3/3）。`^.+_tmp_\d+_[0-9a-f]+$` 理论可误删同名合法目录；已有 `--clean` 显式许可 + `cmd_arg_safe` 两道闸，故 LOW。

**F24 — `dsh-env.py` 缺 stdout reconfigure**（LOW，3/3；注意 `dsh_tests.py` 同样没有，勿表述为唯一漏网）。非 GBK 路径进 report → chcp 936 下 UnicodeEncodeError。修复：补上与其余文件一致的 reconfigure。

---

## INFO 观察（4 项）

- **F25**（3/3）：`dsh_tests.py:579-580, 595` 提交了真实用户名路径（私有仓库）。建议换成 `%USERNAME%` 中性假路径。
- **F26**（3/3）：`start-dsh.bat:2` `enabledelayedexpansion` —— 用户路径含 `!` 会被吞；`chcp 936` 硬编码在非 936 系统全是乱码（与工具链其它 GBK 假设一致，本机无碍）。
- **F28**（3/3）：`dsh-env.cache.json` 非原子写——截断 JSON → 全量重探，fail-safe，可接受（与 F12 的 fail-silent 成对比，勿外推）。
- **F29**（3/3）：Ctrl+C 在 action_check/action_env 期间会退出整个启动器而非回菜单（顶层 `:502-507` 干净退出码 0，**不是**崩溃）。UX 取舍，可改为回菜单。

**被击杀**：~~F27~~（.gitignore 缺 `_*.bat`/`_*.json`——磁盘上无此类文件，纯理论；1/3 存活，降为备注：将来新增临时 bat/json 时记得补模式）。

---

## What was verified

管线：3 名研究员（注入面 / 运行稳定性 / 竞态+secrets，均为只读静态审查）产出 31 个候选 → 去重合并 29 项 → 三镜头验证小组（怀疑论攻击前提与机理、代码溯源核对每条 file:line、Security Lead 汇总裁定）逐项投票，2/3 通过才保留，含 7 处行号/表述修正与两项合并建议（F2+F11 同根；F12+F28 同根不同后果）。secrets pass 覆盖磁盘全部 27 个文件（含 gitignored），命中 12 处并逐一分类（真实凭据 3 处：e2e 日志 token、活动锁 token、5 份备份 token；其余为变量名/文档文本）。计划任务、锁备份、meta 缺失三项做了现场只读核验。**没有执行项目代码、没有触发任何利用**；报告 stamped：`panel-verified (manual emulation) — 非插件官方管线`。

## 建议的处置顺序

1. **今天**：删除计划任务 `dsh-e2e-accept`（F3，今晚 23:59 就触发）；清理 `_launcher-e2e.log` 与 5 份锁备份里的令牌（F4）。
2. **本周**：互斥锁一并修 F11+F2（MEDIUM 根）；`kill_leftover` 护栏（F1）；长操作进度提示 + 线程池早退（F10）。
3. **顺手**：F5/F6/F9/F12/F19 的 try/except 与 isinstance 加固；selfcheck 盲区（F18）与 `_e2e_accept` 击杀闸（F22）。
