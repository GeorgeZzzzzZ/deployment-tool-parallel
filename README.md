# Deploy Console 并行任务中心版

Deploy Console 并行任务中心版是在原版之外独立安装和运行的版本，用于在一个主窗口中同时执行多个 Jenkins 打包与部署任务。它不会修改原版程序或原版配置。

## 底部可折叠任务中心

- 软件只使用一个主窗口。点击标题栏右侧的“新建任务”或按 `Ctrl+N` 会在主窗口内增加独立任务工作区，不再打开新的系统窗口；当前为 SSH 模式时会自动继承目标环境。
- 底部任务中心收起时只显示运行数、失败数和任务总数；点击右侧箭头可展开任务列表。
- 展开后会显示每个任务的包号或 Jenkins Job、目标环境、当前阶段、总体进度和耗时。点击任意任务行即可切换主工作区。
- 任务行右侧的“停止”用于取消正在运行的 Jenkins 一键任务；“移除”用于从任务中心移除已结束或尚未开始的任务。任务中心至少保留一个工作区。
- 新增第二个任务或任一任务失败时，任务中心会自动展开；成功、失败和取消状态同时使用文字与颜色表示。
- 不同 Jenkins 任务可以同时打包；实际并行数量仍受 Jenkins 执行器和任务自身并发设置限制。
- 不同 SSH 或云端目标可以同时部署。
- 默认情况下，相同 SSH 目标或相同云端目标会自动进入安全队列，前一个部署结束后再继续，避免相互覆盖。
- SSH 一键任务自动按可共享镜像搬运命令的目标环境合批：多个任务先并行打包，再由一次镜像搬运统一处理本批次包，随后逐个更新并应用对应 YAML。
- 一个任务工作区同一时间只执行一个打包或部署操作；需要并发时必须新增任务，避免日志和状态相互覆盖。
- 任一任务仍在运行或加载数据时不能退出软件，需先取消并等待结束。
- 所有任务共享并行版配置对象，防止任务之间保存配置时相互覆盖。

项目详细背景、架构、关键约束和开发注意事项见 [HANDOFF.md](HANDOFF.md)。新成员修改代码前必须先阅读该文档。

## 功能

- 浏览 Jenkins 文件夹任务，读取构建参数并记忆每个任务的参数值。
- 触发 Jenkins 构建，持续显示控制台日志、预计进度和最终结果。
- 从构建日志中提取镜像包号。
- 通过 SSH 更新镜像清单和 Kubernetes YAML，并执行环境发布命令。
- 管理多套部署环境及其关联的 Kubernetes Dashboard。
- 双击环境名称时使用系统默认浏览器打开环境页面，同时复制已保存的环境密码并提示账号。
- Windows 双击 Dashboard 地址时使用 Edge 自动填写 Token，登录成功后直接进入 `etm` 命名空间的 Pods 页面；失败时回退为默认浏览器并复制 Token。
- 登录鼎捷 Ops 平台，选择部署专案、服务类型和云区节点后发布 Jenkins 构建包。
- Ops 云端部署成功后自动打开 Rancher Pod 状态页，并使用已保存的账号密码登录。
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

1. 首次使用时填写 Jenkins 地址、账号和密码或 API Token，然后点击“登录并加载”。
2. Windows 版会在凭据完整时自动登录：首次使用或没有匹配缓存时读取完整任务树并持久化，之后启动直接使用本地缓存，不再重复递归请求 Jenkins。
3. Jenkins 任务有变化时，由用户点击搜索框右侧的刷新按钮重置并重新缓存；缓存按 Jenkins 地址和账号隔离，不会跨服务器误用。
4. Windows 版可模糊搜索根任务下的二级模块，命中模块的全部子任务会保留在结果树中。
5. 在任务树选择最终构建任务并设置构建参数。
6. 选择 `SSH 环境部署` 或 `云端部署`；关闭“打包后部署”开关时仅执行 Jenkins 打包。

任务树中的目录可点击整行展开或收起，也可选中后按 `Enter`、`Space` 操作，不必精确点击左侧箭头。
7. 点击当前模式对应的执行按钮。

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

同一批次若多个包指向同一个 YAML，最后登记的包生效，较早任务会在部署日志中显示被覆盖提示。不同 YAML 会在镜像统一搬运完成后分别更新并执行各自的 `kubectl apply`。

### Ops 云端部署

1. 在“云端部署”页填写平台地址、账号和密码。
2. 在“云环境部署状态（Rancher）”区域填写 Pod 状态页地址、账号和密码，可先点击“保存并测试登录”验证。
3. 登录并加载部署专案、服务类型和云区节点；工具直接调用 Ops API，不会为 Ops 登录启动浏览器。
4. 在“环境部署与 Dashboard”页输入包号并按回车，工具会通过 Ops 查询并切换到对应的实际部署专案和服务类型，同时沿用原有云区节点选择逻辑。例如普通 `backend/frontend` 映射到产品专案，`etmmobilefrontend` 映射到 `etmmobile / etmmobile 前端`。
5. Jenkins 构建成功后，可直接发布当前包，或从 Jenkins 页执行一键云端部署。
6. 提交前会精确校验部署目标和镜像版本，提交后会确认产生新的部署记录，并自动打开 Rancher Pod 状态页登录查看。

为避免重复发布，部署 POST 不会自动重试。如果平台已接收请求但客户端无法确认新记录，界面会显示“部署结果未知”；此时必须先到 Ops 平台核对记录，不能直接重试。

## 测试

```powershell
python -m py_compile main.py core.py ops_api.py test_core.py test_package_inference.py test_parallel_windows.py
python -m unittest test_core.py test_package_inference.py test_parallel_windows.py -v
```

测试不得连接真实 Jenkins、SSH 或 Ops 环境，外部操作必须使用 Mock。

## 打包

Windows：

```powershell
.\build.bat
```

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
Windows: dist/DeployConsoleBatchSafe.exe
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
