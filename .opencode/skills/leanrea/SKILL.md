# leanrea（条件技能 · 首轮逆向侦察加速器）

> **性质**：条件触发。仅在任务目标命中「逆向工程」特征（二进制 / 安装包 / 固件 /
> app.asar / .apk / .dll / ELF / firmware 等）时由 dispatch-agent 注入本路由表；
> 普通开发任务**不加载**本 skill（见 dispatcher `_detect_reverse`）。
> **这是首轮侦察加速器，不是全自动逆向工具**：只生成「首条最该执行的命令」，
> 实打实的逆向仍需人类或后续专门流程。

## 触发时的注入指令（dispatcher 自动前置）

当判定为逆向任务，dispatcher 会在派活 prompt 前注入：

```
请先读取 .opencode/skills/leanrea/SKILL.md，按其「路由表」为本次逆向目标选择首条命令。
务必先把目标文件复制到 worktree 副本（例如 worktrees/task-<id>/_reverse/copy），
再对副本操作，绝不触碰原始路径。
```

## 路由表（按目标类型选首条命令）

| 目标类型 | 识别特征 | 首条命令（对副本执行） | 说明 |
|----------|----------|------------------------|------|
| Electron 应用 | `app.asar` / `.asar` | `npx @electron/asar extract copy/app.asar copy/app.asar.unpacked` 后 `rg -uu "关键词" copy/app.asar.unpacked` | 解包 asar 再全文 grep（`-uu` 看二进制匹配） |
| Go 二进制 | `.elf` / ELF / 无扩展可执行 | `go version -m copy/<bin>` | 读 Go 构建信息 / 内嵌路径（需 go 在 PATH） |
| macOS 可执行 | Mach-O / `.app` | `otool -l copy/<bin>`、`nm copy/<bin>`、`dyld_info -exports copy/<bin>` | 段表 / 符号 / 导出 |
| Android | `.apk` / `.dex` | `apktool d copy/app.apk -o copy/app.dec` 后 `rg -uu "关键词" copy/app.dec` | 反编译 apk |
| Windows | `.dll` / `.exe` | `dumpbin /headers copy/<bin>` 或 `strings copy/<bin>` | PE 头 / 字符串 |
| 固件 | `firmware` / `.bin` / `.img` | `binwalk copy/<fw>` 或 `strings copy/<fw>` | 镜像结构 / 字符串 |

## 硬规则（违反即返工）

1. **先复制，后对副本操作**：任何目标文件在审阅前必须先 `copy` 到 worktree 的
   `_reverse/copy/` 目录；对 `copy/` 之外（尤其原始路径、项目产出 `work/`）写操作一律禁止。
2. **不整体读大文件入上下文**：逆向目标常是 MB 级二进制，禁止把整个文件塞进 prompt；
   只用 `rg -uu` / `strings` / `binwalk` 等带模式扫描，取命中片段。
3. **只做侦察，不做改动**：本路由下 OpenCode 不要改业务代码、不要删原始文件；
   产物只落在 `worktrees/task-<id>/_reverse/` 与 `work/` 下的侦察报告。
4. **缺工具降级**：`capabilities.json` 的 `reverse_toolchain` 探测不到对应工具时，
   记 `capability_gap`，不阻塞，降级到通用 OpenCode 尝试（可能在副本上做启发式 grep）。

## 后续（可选，官方增强）

> **此处可粘贴官方 8KB SKILL.md**（如有更完整的逆向方法论）替换本文件；接口不变：
> dispatcher 仍只注入「读取本 SKILL.md 并选首条命令」，规则同上。
