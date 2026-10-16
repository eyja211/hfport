# hfport

**简体中文** | [English](README.en.md) | [日本語](README.ja.md)

**Hero Fighter 角色移植工具**用于在兼容的 Hero Fighter 游戏版本与 mod
之间移植玩家可选角色。它会分析角色依赖、处理资源重名、补齐目标版本的
注册入口，并生成一个新的 EXE，不覆盖源文件或目标文件。

> 本仓库只发布工具源码，不包含 Hero Fighter EXE、SWF、游戏资源、
> FFDec 或任何移植后的游戏成品。

## 移植内容

- 角色 SPT 数据与 LMI 贴图
- `globalDat` 中角色引用的攻击数据与 Pt 模板
- 角色使用的 item 动作组、飞行道具和贴图的独立副本
- 目标版本中不存在的角色音效资源
- “角色一览”和 VS 选人列表注册
- 绑定到目标版本已有角色的经验数据

每次移植都会根据源文件名生成一个简短命名空间。如果目标版本已经使用该
命名空间，hfport 会自动分配下一个编号。角色引用的 item 组会打包成独立的
item SPT/LMI，并在运行时按来源路由：即使源和目标使用相同的原始 `int1`，
也不会误用目标 mod 修改过的同下标 item，目标原有的 `itemSpt` 和 `itemLmi`
保持不变。

## 原有角色引用移植贴图

新增的角色 LMI 和 item LMI 会在目标版本原有 SPT 读取之前注册，因此目标
版本原有的角色和 item 也可以在 HFWorkshop 中通过 `spriteLimb.limbName`
（或换色用的 `limbNames`）引用移植过来的贴图：

- 填写输出 LMI 中实际的 `Limb.name`，包括 hfport 追加的 `@命名空间` 后缀，
  而不是资源类名或 `.lmi` 文件名。
- 帧所使用的贴图槽位必须在该 limb 中存在。
- 只修改 `lmiFiles` 不会建立运行时引用。

此规则适用于新版 hfport 生成的资源。旧版 hfport 生成的 EXE 不会被自动修复，
需要用新版重新生成。

## 兼容范围

hfport 面向 Hero Fighter 系列及采用兼容 Flash 数据布局的衍生 mod，
不按某个 mod 名称或单一版本号写死。

当前实现要求输入为内嵌 SWF 的 Windows Flash projector，并保留常见的
Hero Fighter 数据与注册结构：

- `Data.Global_*` 二进制资源，包括 `globalDat`、`itemSpt` 和 `itemLmi`
- `Data.BgInfoFile` 数据加载入口
- `Data.Global` 角色注册与经验初始化
- `Web_misc.SelectCharPanel` 选人列表
- `Page.CharList` 角色一览翻页逻辑

不同版本或深度修改的 mod 可能改变这些结构。请始终使用目标文件副本，
先通过工具的静态验证，再完成游戏内测试。本工具不能保证兼容所有
Hero Fighter 版本和 mod。

## 环境要求

- Windows
- Python 3.8 或更高版本，仅使用标准库
- Java
- [JPEXS Free Flash Decompiler（FFDec）](https://github.com/jindrapetrik/jpexs-decompiler)，
  用于提取工具需要的少量注册代码

将 `ffdec.jar` 放在 `FFDec/ffdec.jar`，或在配置文件中指定其路径。
FFDec 和游戏文件应留在 Git 仓库之外，项目提供的 `.gitignore` 已覆盖
常见的本地目录结构。

## 快速开始

1. 克隆本仓库。
2. 准备 `FFDec/ffdec.jar`，或记录它的绝对路径。
3. 在本地工作目录中准备源版本和目标版本 EXE 的副本。
4. 双击 `移植工具.bat`，或者运行：

   ```powershell
   python -m hfport.cli
   ```

5. 选择源版本、目标版本和需要移植的角色。已经由 hfport 扩展过的版本，
   也可以继续作为源或目标。
6. 查看静态验证结果，并在游戏内完成实测。

工具默认将新文件写入 `output/`，不会覆盖所选的源 EXE 或目标 EXE。

## 配置运行

参考 `examples/port-config.example.json` 创建本地配置，替换其中的示例
路径后运行：

```powershell
python -m hfport.cli --config path\to\port-config.json
```

只列出源版本中的可选角色，不执行移植：

```powershell
python -m hfport.cli --list "C:\path\to\source-build.exe"
```

## 验证

构建完成后，hfport 会检查生成的 projector/SWF 结构、ABC 往返一致性、
新增资源、主要数据引用、角色注册字符串，以及新增贴图与 SPT 的加载阶段。通过静态验证只是必要条件，
最终仍应在游戏中检查：

- 游戏启动和战斗加载
- 角色一览与 VS 选人
- 动作、攻击和飞行道具
- item 行为、贴图和音效
- 经验绑定
- 目标版本原有角色是否正常

`hfport/tests/` 中的部分集成测试需要本地游戏文件和 FFDec，这些文件不会
随公开仓库提供。任何环境都可以运行以下源码检查：

```powershell
python -m compileall hfport
python -m hfport.cli --help
```

FFDec 导出的注册清单会按输入 EXE 的完整 SHA-256 分目录缓存，因此切换
不同版本时不会误用其他游戏留下的 `pow`、经验绑定或选人数据。

## 目录结构

```text
hfport/                 解析、移植、注册补丁、验证与 CLI
hfport/tests/           单元测试及需要本地游戏文件的集成测试
examples/               已脱敏的配置示例
移植工具.bat             Windows 双击启动入口
work/                   自动生成的中间文件（Git 忽略）
output/                 自动生成的移植产物与配置（Git 忽略）
```

## 安全说明

- 始终使用源文件和目标文件的副本。
- 除非拥有合法分发权，否则不要发布游戏 EXE、SWF、提取资源或移植成品。
- 联机玩家需要使用完全相同的生成版本。
- 源 mod 中通过 AS3 引擎代码实现的特殊机制不属于数据移植范围，移植后
  仍按照目标版本的引擎逻辑运行。

## 参与贡献

提交 issue 或 pull request 前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。
请勿在仓库、issue 或 pull request 中上传受版权保护的游戏文件或提取资源。

## 许可证

本仓库源码采用 [MIT License](LICENSE)。Hero Fighter、各游戏版本、游戏
资源和第三方工具的权利归各自所有者所有。本项目与相关权利方没有隶属或
背书关系。
