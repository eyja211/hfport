# Hero Fighter 角色移植工具（hfport）使用说明

公开仓库的完整说明、兼容边界和贡献规则请优先阅读 [`README.md`](README.md)。

hfport 面向采用兼容 Flash/SWF 数据布局的 Hero Fighter 游戏版本与 mod，
不是按某个特定 mod 名称或单一版本标签写死。它把一个**玩家可选角色**完整移植到另一个兼容目标：
角色 SPT + 贴图 LMI + globalDat 攻击数据 + itemSpt/itemLmi 飞行道具及贴图 +
用到的差异音效，自动加入"角色一览"、VS 选人列表，并把经验绑定到已有角色防止封号。
每次移植会从源文件名自动生成内部命名空间；目标已存在时自动加 2、3。
肢体/贴图/攻击/道具组重名会自动改名并重接线，不会触发游戏重名警告，
也不会影响目标 mod 原有角色。

角色用到的 item 无论是否与目标同下标、同名或同内容，都会复制为新的
actionGroup、动作、帧、spriteLimb、LMI 和 PNG。比如源和目标的 `lucasB`
都使用 `int1=5`，移植后也会把源 `lucasB` 复制到新下标，不会复用目标第 5 组。

兼容的前提是输入为带内嵌 SWF 的 Windows Flash projector，且目标仍保留
`Data.Global`、`Data.BgInfoFile`、`Web_misc.SelectCharPanel` 和 `Page.CharList`
等 Hero Fighter 常见数据及注册布局。改动很大的版本需要先在副本上试跑，完成静态验证和游戏实测；
本工具不承诺适配所有 Hero Fighter 版本或 mod。

## 一键使用

双击 `移植工具.bat`，按提示：

1. 选"源"EXE（要移植谁的角色）
2. 选"目标"EXE（移植进哪个兼容版本或 mod）
3. 从完整列表勾选要移植的角色（回车默认全部；已移植版本的追加角色也会显示）
4. 确认显示名后缀（默认根据源文件名生成，用来在选人界面区分移植角色）
5. 等待完成——输出在 `output\` 下，**绝不覆盖原 EXE**

跑完会自动做静态验证并打印游戏实测清单。配置会存到
`output\last_port_config.json`，下次可以：

    python -m hfport.cli --config output\last_port_config.json

只看源里有哪些可选角色：

    python -m hfport.cli --list "某个mod.exe"

## 依赖

- Python 3.8+（无第三方库）
- Java + FFDec（将 `ffdec.jar` 放到 `FFDec\ffdec.jar`，或在配置中指定路径；仅用于导出注册数据文本）

## 它改了游戏里的什么

| 位置 | 内容 |
|---|---|
| 新增 DefineBinaryData + 资产类 | 每个角色及 refSpt 依赖的 SPT/LMI（类名带本次命名空间）|
| Data.Global_globalDat | 追加移植角色用到的攻击/Pt 模板（重名时加命名空间后缀）|
| Data.Global_itemSpt / itemLmi | 强制追加源飞行道具动作组/帧/贴图并整体重编号 |
| `LoadGlobalDatFromCompressedBytes` 尾部 | 注册 pow / charListOrder / langWords / 音效表 |
| `BgInfoFile.LoadFromCompressedBytes` 尾部 | 加载新 LMI/SPT（按 refSpt 依赖排序）|
| `InitPow` 尾部 | `pow["新角色ID"].c = pow["绑定角色"].c` 经验绑定 |
| `SelectCharPanel` 类初始化尾部 | VS 选人列表追加 |
| `Page.CharList` 翻页 | 角色一览按最终人数自动扩页（每页 15 格）|

## 已知边界

- **引擎代码不移植**：若源 mod 的某机制是改 AS3 引擎实现的（不是数据），
  移植后按目标 mod 引擎表现。
- 源角色若缺少 pow、SPT 或合法经验绑定，工具会在预检阶段停止并列出缺失项，
  不会擅自回退到 Lucas。
- **BGM 不移植**（角色实际用到的差异战斗音效会作为新增音效自动移植）。
- 雇佣兵用目标版本现有的同名士兵。
- 联机对战需双方使用同一个移植后的 EXE。
- 加载画面末尾会多几秒读条（移植数据一次性载入）。

## 目录结构

    hfport/          工具本体（amf/swf/abcpatch/gamedata/porter/registry/builder/verify/cli）
    hfport/tests/    集成自检脚本（需要本地游戏文件和 FFDec；公开仓库不提供）
    移植工具.bat      双击入口
    output/          移植产物与配置
    work/            中间文件（自动重建，已由 Git 忽略）

`work/registry/` 下的 FFDec 注册清单按输入 EXE 的 SHA-256 分目录缓存，不同版本
不会再误用之前运行留下的 `reg_src/reg_dst`。
