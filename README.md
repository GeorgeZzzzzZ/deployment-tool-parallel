# Deploy Console 并行任务中心版

Deploy Console 并行任务中心版是在原版之外独立安装和运行的版本，用于在一个主窗口中同时执行多个 Jenkins 打包与部署任务。它不会修改原版程序或原版配置。

界面沿用迁移工具的深色渐变与雾面玻璃卡片，悬停过渡为 200ms，并尊重 Windows 减少动画设置。图标随程序离线打包，来源与许可见 `assets/lucide/LICENSE` 和 `assets/uiverse/SOURCE.md`。

## 底部可折叠任务中心

- 软件只使用一个主窗口。点击标题栏右侧的“新建任务”或按 `Ctrl+N` 会在主窗口内增加独立任务工作区，不再打开新的系统窗口；当前为 SSH 模式时会自动继承目标环境。
- 同一用户配置目录只运行一份应用，重复双击程序会恢复已有主窗口，不再新增托盘图标；正常退出或异常结束后可重新启动。首次更新时需先通过托盘菜单退出旧版。
- 底部任务中心收起时只显示运行数、失败数和任务总数；点击右侧箭头可展开任务列表。
- 点击任务的包号可复制完整内容，也可选中任务后按 Ctrl+C；环境部署表单在可用高度不足时支持滚动。
- 展开后会显示每个任务的包号或 Jenkins Job、目标环境、当前阶段、总体进度和耗时。点击任意任务行即可切换主工作区。
- 任务行右侧的“停止”用于取消正在运行的 Jenkins 一键任务；等待确认期间显示禁用的“停止中”，确认取消且后台收尾完成后变为“继续”。点击“继续”会按上次启动时的 Job、分支、参数及部署配置重新打包，并重置进度、耗时和日志；不使用后来修改的表单值。仅打包/打包后部署开关也沿用原值，SSH 合批会重新登记。继续所需配置只保留在本次应用会话中。“移除”用于移除已结束或尚未开始的任务，任务中心至少保留一个工作区。
- 新增第二个任务或任一任务失败时，任务中心会自动展开；成功、失败和取消状态同时使用文字与颜色表示。
- 不同 Jenkins 任务可以同时打包；实际并行数量仍受 Jenkins 执行器和任务自身并发设置限制。
- 不同 SSH 或云端目标可以同时部署。
- 默认情况下，相同 SSH 目标或相同云端目标会自动进入安全队列，前一个部署结束后再继续，避免相互覆盖。
- SSH 一键任务自动按可共享镜像搬运命令的目标环境合批：多个任务先并行打包，再由一次镜像搬运统一处理本批次包，随后逐个更新并应用对应 YAML。
- 一个任务工作区同一时间只执行一个打包或部署操作；需要并发时必须新增任务，避免日志和状态相互覆盖。
- 点击主窗口右上角“×”会隐藏到 Windows 系统托盘，后台任务不会中断；单击托盘图标即可恢复。需要真正退出时使用托盘菜单或“配置迁移 → 退出应用”，运行中的任务仍需先结束。
- 所有任务共享并行版配置对象，防止任务之间保存配置时相互覆盖。

项目详细背景、架构、关键约束和开发注意事项见 [HANDOFF.md](HANDOFF.md)。新成员修改代码前必须先阅读该文档。

## 功能

- 浏览 Jenkins 文件夹任务，读取构建参数并记忆每个任务的参数值。
- 触发 Jenkins 构建，持续显示控制台日志、预计进度和最终结果。
- 从构建日志中提取镜像包号。
- 通过 SSH 更新镜像清单和 Kubernetes YAML，并执行环境发布命令。
- 管理多套部署环境及其关联的 Kubernetes Dashboard。
- 双击环境名称时使用系统默认浏览器打开环境页面，同时复制已保存的环境密码并提示账号。
- Windows 一键 SSH 部署后使用 Edge 自动登录 Dashboard，并按最终部署包的产品打开对应命名空间的 Pods 页面（如 PQM → `pqm`、ESC → `esc`、ETM → `etm`）；合并部署按环境和产品去重，同一产品前后端只打开一次。失败时回退为默认浏览器并复制 Token，保留本次目标命名空间。手动双击 Dashboard 地址或点击“自动登录”仍默认打开 `etm`。
- 登录鼎捷 Ops 平台，选择部署专案、服务类型和云区节点后发布 Jenkins 构建包。
- Ops 云端部署成功后自动打开 Rancher Pod 状态页，并使用已保存的账号密码登录。
- Dashboard 与 Rancher 自动登录成功后只更新状态和日志，不再显示需要确认的成功提示框。
- 支持 Jenkins 一键打包后转 SSH 环境部署或 Ops 云端部署。
- 支持取消 Jenkins 排队或构建，并确认线上任务真实停止。
- Jenkins、SSH 和 Ops 运行失败时，错误提示固定显示在当前任务工作区内，不再弹出独立系统对话框打断其他并行任务。

## 环境要求

- Windows 10/11、macOS 13+ 或 Deepin 25 AMD64
- Python 3.11/3.12（仅源码运行和本机打包需要）
- Windows 使用 Microsoft Edge；macOS/Deepin 使用 Google Chrome 或 Chromium
- 可访问目标 Jenkins、SSH 环境、Dashboard 和 Ops 平台的网络

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

## 本地运行

```powershell
python main.py
```

应用配置按平台保存于：

```text
Windows: %APPDATA%\BuildDeployToolParallel\config.json
macOS:   ~/Library/Application Support/BuildDeployToolParallel/config.json
Deepin:  ${XDG_CONFIG_HOME:-~/.config}/BuildDeployToolParallel/config.json
```

Windows 首次启动并行版时会只读复制原版 `%APPDATA%\BuildDeployTool\config.json`，之后仅保存到并行版目录。

Windows 凭据使用 DPAPI 加密；macOS 和 Deepin 使用仅当前用户可读写的本地密钥文件加密，避免 Keychain 或桌面 Secret Service 未启动、未解锁时阻塞登录按钮。不同操作系统之间不能直接迁移已保存的密文凭据，需要重新输入密码和 Token。

## 基本使用

### Jenkins 打包

1. 首次使用时填写 Jenkins 地址、账号和密码或 API Token，然后点击“登录并加载”。每次登录先向 Jenkins 验证身份，成功并加载任务后显示“登录成功”，Windows 版才收起连接栏；失败保持展开并显示具体错误（如 HTTP 401、403 或连接异常），已有本地缓存不会被当作登录成功。
2. Windows 版会在凭据完整时自动登录：身份验证通过后只读取任务树，已有任务树缓存则直接复用。构建参数和分支在首次点击具体任务时读取并缓存，之后点击、新建任务或重启均优先复用缓存；不会在启动时遍历所有任务预加载，也不会等待云端目标读取。
3. 点击搜索框右侧的刷新按钮只更新完整任务树，不受当前搜索结果限制，已有参数和云端目标缓存保留。分支使用参数旁的“刷新分支”更新，云端目标使用对应刷新按钮更新。缓存按平台地址和账号隔离，不会跨服务器误用。
4. Windows 版可模糊搜索根任务下的二级模块，命中模块的全部子任务会保留在结果树中。
5. 在任务树选择最终构建任务并设置构建参数；构建参数默认收起，保留可编辑的 `GIT_BRANCH`，修改或刷新分支不会展开其他参数。点击标题可展开全部参数；`GIT_BRANCH`/`GIT_BRANCH2` 旁的“刷新分支”可单独获取最新分支并更新缓存，不会重置其他已填写参数。`GIT_BRANCH2` 刷新时复用当前任务的 `GIT_BRANCH` 分支列表，保留自身已填写值，也可从下拉框搜索选择；留空时仍由二开打包流程使用 `GIT_BRANCH`。首次读取失败时，再次点击该任务可重试。
   云端部署目标配置可点击标题收起。“状态集”默认展开，打包前不加载历史记录；本工具发起打包并取得构建编号后，显示本次结果、实际分支、提交版本和 Changes 变更记录，构建中每 5 秒更新，完成后停止。切换模块或任务时只显示各自本次打包记录；排队取消且未取得编号时不显示其他构建。手动刷新和“在 Jenkins 查看”始终对应本次编号，记录仅保留在当前程序会话。
6. 选择 `SSH 环境部署` 或 `云端部署`；关闭“打包后部署”开关时仅执行 Jenkins 打包。

任务树中的目录可点击整行展开或收起，也可选中后按 `Enter`、`Space` 操作，不必精确点击左侧箭头。选择模块或具体服务时，Jenkins 页和云端部署页的部署专案自动切到对应模块；WEB/SERVICE 任务同时匹配前端/后端，APP 任务匹配对应的 mobile 专案及前端，例如 ETM 的 `2-APP` 对应 `etmmobile`。目标仍可手动修改，APP 任务可使用同系统的 mobile 专案，其他系统仍禁止混用。模块文件夹不加载构建参数。
7. 点击当前模式对应的执行按钮。

Jenkins 任务路径和打包操作栏固定在右侧顶部，滚动配置、状态集和日志时仍可操作。顶部页签可按住拖动排序，顺序同步到其他任务并在重启后保留；自动跳转始终进入对应功能页。

### SSH 环境部署

1. 在“环境部署与 Dashboard”页新增 Dashboard。
2. 新增部署环境并关联 Dashboard。
3. 输入包号并按回车，工具会自动识别后端、前端或移动端包，并填写 YAML 目录、YAML 文件和应用命令；这些临时值只有在用户主动保存环境时才会写入配置。
4. 核对镜像目录、镜像清单、YAML 和执行命令。
5. 保存环境配置后，从 Jenkins 页执行一键流程，或在环境页直接执行部署。

执行 SSH 环境部署时，每个任务都会在镜像目录下创建独立的临时工作目录和单包镜像清单，复用原拉取脚本后立即清理。不同产品可以并行搬运镜像，不会再争用或覆盖环境保存的公共镜像清单。

SSH 镜像搬运期间，部署日志会过滤终端动态重绘字符，并区分显示从源仓库拉取、向目标仓库推送的传输进度。远程输出采用大块高速读取和节流刷新，避免大量 Docker 进度行拖慢 SSH 通道；日志还会记录镜像拉取及推送耗时与部署总耗时。

SSH 密码认证最多等待 30 秒，以兼容认证响应较慢的目标环境；网络连接和服务握手仍分别限制在 15 秒内，避免不可达主机长时间阻塞任务工作区。

一键 SSH 流程会以 Jenkins 最终产出的包号重新计算 YAML 目录、文件和应用命令，部署前先校验 YAML 中的镜像名。目标 Harbor 已存在完全相同的镜像时会跳过重复拉取和推送；同一环境的 Jenkins 构建可并行，但 SSH 镜像搬运和 YAML 发布会安全排队，避免共享镜像清单相互覆盖。

SSH 一键任务开始后会自动登记批次，无需开关或再次确认。程序按 SSH 主机、端口、账号、镜像目录、镜像清单和拉取命令判断是否可合批，等待本批次所有已登记任务打包完成后自动生成一份多包镜像清单统一搬运。若新任务进入时上一批处于源镜像拉取阶段，软件会在当前工作区内询问：可继续当前轮，或安全终止受控镜像命令、等待新包完成后重新合批；推送、YAML 修改和 `kubectl apply` 阶段不会中断，只会进入下一轮。需要继续增加任务时，从标题栏点击“新建任务”或按 `Ctrl+N` 即可继承当前 SSH 环境。

合批搬运由关闭作业控制的 Shell 启动 `setsid`，再用 Shell `wait` 等待脚本完成并检查其退出码，兼容不支持 `setsid --wait` 的旧主机。脚本非零退出时停止后续镜像确认与 YAML 发布。搬运后的镜像探测会区分仓库返回未找到、认证/权限、DNS、网络、TLS 和 Docker CLI 错误，日志仅显示错误类别与退出码，超时保留最后一次诊断，不显示可能含凭据的原始响应。

同一批次若多个包指向同一个 YAML，最后登记的包生效，较早任务会在部署日志中显示被覆盖提示。不同 YAML 会在镜像统一搬运完成后分别更新并执行各自的 `kubectl apply`。

### Ops 云端部署

1. 在“云端部署”页填写平台地址、账号和密码。
2. 在“云环境部署状态（Rancher）”区域填写 Pod 状态页地址、账号和密码，可先点击“保存并测试登录”验证。
3. 点击“登录并加载部署数据”后，通过 Ops API 加载部署专案、服务类型和云区节点，并打开浏览器填写已保存的账号密码、自动登录 Ops 网页。刷新目标及后台部署仍直接调用 API；网页自动登录失败会提示原因并保留已加载的数据。
4. 首次加载会预先缓存当前云厂商下全部专案的服务、云区选项，未选择过的专案也会读取；完整缓存支持重启和新建任务复用。点击“刷新云端目标”“刷新三个栏位”或重新登录会全量重新读取。缓存按平台地址、账号、云厂商和专案隔离，个别查询失败保留旧缓存并提示，其他成功专案可正常使用。原云区在新服务中仍有效时保留，否则清空并由用户重新选择。缓存只保存界面选项，实际部署仍通过 API 登录并校验目标和镜像。
5. “云端部署”页的包号默认带出最近的 Jenkins 构建包，可直接编辑，点击“部署此包号”时以该输入框为准。输入后按 Enter 可匹配实际专案和服务类型；例如 `etmmobilefrontend` 对应 `etmmobile / etmmobile 前端`。“环境部署与 Dashboard”页输入包号按 Enter 的联动也继续支持。
6. 提交前会精确校验部署目标和镜像版本，提交后会确认产生新的部署记录，并自动打开 Rancher Pod 状态页登录查看。

为避免重复发布，部署 POST 不会自动重试。如果平台已接收请求但客户端无法确认新记录，界面会显示“部署结果未知”；此时必须先到 Ops 平台核对记录，不能直接重试。

## 测试

```powershell
python -m py_compile main.py core.py ops_api.py test_core.py test_package_inference.py test_parallel_windows.py test_ui_regressions.py
python -m unittest test_core.py test_package_inference.py test_parallel_windows.py test_ui_regressions.py test_cloud_targets.py test_selection_cache.py test_full_cache.py -v
```

测试不得连接真实 Jenkins、SSH 或 Ops 环境，外部操作必须使用 Mock。

部署等待与取消回归：`python -m unittest test_remote_deployment -v`。Windows 会通过已安装的 WSL `Ubuntu-24.04` 执行真实 PTY 检查；Linux 可直接运行。该检查只在本机临时目录执行测试 shell，验证成功、非零退出、进程组取消和合批隔离，不连接远程环境。测试文件按原样加载部署方法，WSL 无需安装 Qt 或 SSH 依赖。

## 打包

Windows：

```powershell
.\build.bat
```

Windows 当前发布入口统一为 `release/DeployConsoleBatchSafe.exe`；对应源码、资源与测试保留在项目根目录。打包脚本会更新该入口并执行凭据和 Selenium 自检。

macOS（必须在 Mac 上执行）：

```bash
bash ./build-macos.sh
```

Deepin 25：

```bash
bash ./build-deepin.sh
```

对应产物：

```text
Windows: release/DeployConsoleBatchSafe.exe
macOS:   dist/DeployConsoleParallel.app、dist/DeployConsoleParallel-macos.dmg
Deepin:  dist/DeployConsoleParallel、dist/DeployConsoleParallel-deepin25-x86_64.tar.gz
```

macOS 正式分发前应设置 `CODESIGN_IDENTITY` 完成签名，并按组织发布流程执行 Apple 公证。

## 项目结构

```text
deployment-tool/
├─ main.py                 # UI、Jenkins、SSH、Ops、Dashboard 主流程
├─ core.py                 # 可独立测试的纯业务函数
├─ test_core.py            # 单元测试
├─ DeployConsole.spec      # 唯一 PyInstaller 配置
├─ build.bat               # Windows 打包入口
├─ build-macos.sh          # macOS .app/.dmg 打包入口
├─ build-deepin.sh         # Deepin Linux 打包入口
├─ requirements.txt        # Python 依赖
├─ HANDOFF.md              # 开发交接文档
├─ design-system/          # UI 设计规范
└─ dist/                   # 各平台运行包
```

## 安全提示

- 云端部署和 SSH 部署会改变运行环境，操作前必须核对目标和包号。
- 不要在源码、文档、日志或提交中写入密码、Token 和其他凭据。
- 不要绕过现有确认弹窗、环境保存检查和目标完整性检查。
- Ops 页面结构变化时应增强定位与错误上下文，不能静默选择其他包或区域。

## 平台说明

- PyInstaller 不能跨操作系统生成原生包：Windows EXE、macOS `.app` 和 Linux ELF 必须分别在对应系统构建。
- 当前已验证 Windows 与 Ubuntu/WSL AMD64 构建；Deepin 包目标为 Deepin 25 AMD64，仍建议在真实 Deepin 25 桌面执行一次浏览器与剪贴板验收。
- macOS 构建脚本已就绪，但必须在真实 Mac 上生成并验证 `.app/.dmg`；未签名包可能被 Gatekeeper 阻止。
- macOS 与 Deepin 首次使用时需要重新录入原 Windows 配置中的密码和 Token。
