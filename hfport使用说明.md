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

移植的 item 整套装进**独立的一对文件**：`spt_data/<命名空间>_item.spt` 和
`lmi_data/<命名空间>_item.lmi`，注册成 `Global.sptHash["item_<命名空间>"]`。
目标原有的 `Data.Global_itemSpt` 和 `Data.Global_itemLmi` **逐字节不变** ——
itemLmi 通常是整个包里最大的一个文件，混进移植贴图会让以后用 HFWorkshop
换贴图重新导入变得很慢。

独立包里的组号从 0 密排，组名保持源版本原样（不进 `iteHash`，顶不掉目标同名组）。

### cre 与 Pt 模板：移植的是"源里实际跑出来的行为"

`Frame.cre[]` 里的 `int1` 选择 item 的 actionGroup，但 `ref >= 0` 时游戏
（`Data.Spt.CheckCopyRefPt`）会用 `Global.pt[ref]` 这个模板**覆盖**
`facing/vx/vy/vz/int1/int2/int3`。源和目标的 Pt 表几乎不可能对齐，所以工具会：

1. 按源版本的运行时语义把每条 cre 解析出最终值（模板存在就套用，`ref` 越界就用
   cre 自身的值——**源里 `Global.pt` 是空表时越界才是正常情况**）；
2. 把这些最终值内联写进 cre，并把 `ref` 置 `-1`、`refName` 置空，
   让目标的 Pt 表再也碰不到它；
3. 把最终 `int1` 重映射到独立包里的局部组号，并写上 `itemSptRoute`。

也就是说**以源版本里玩家实际看到的行为为准**，而不是照搬 `ref` 去猜作者本意。
如果某个源 mod 自己的 Pt 表丢了、导致它的 `ref` 在源里本就不生效，那么移植结果
会忠实复制"不生效"这个事实。构建日志里的 `pt-ref-out-of-range` 警告统计就是这类
cre 的数量。

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
| hfport/ 模块 | `itemroute.py` = 钩子 F（独立 itemSpt 的运行时路由）|
| Data.Global_itemSpt / itemLmi | **不改**，逐字节保持原样 |
| 新增 `<命名空间>_item.spt` / `_item.lmi` | 移植 item 的组/动作/帧/limb/PNG 独立一套，注册成 `sptHash["item_<命名空间>"]` |
| `LoadGlobalDatFromCompressedBytes` 尾部（钩子 A）| 注册 pow / charListOrder / langWords / 音效表，并提前加载全部新增 LMI |
| `BgInfoFile.LoadFromCompressedBytes` 尾部（钩子 B）| 只加载新增 SPT（保持 refSpt 依赖顺序）|
| `InitPow` 尾部 | `pow["新角色ID"].c = pow["绑定角色"].c` 经验绑定 |
| `SelectCharPanel` 类初始化尾部 | VS 选人列表追加 |
| `Page.CharList` 翻页 | 角色一览按最终人数自动扩页（每页 15 格）|
| 新增槽位（钩子 F）| `Data.Pt.itemSptRoute`、`Game.ObjI.itemSptRoute`、`Game.World.hfpRoute` |
| 新增方法（钩子 F）| `Data.Global.ResolveItemSpt(route)` |
| 改方法（钩子 F）| `World.AddObjI`、`World.ReboundBlast`、`ObjH::Run` ×2、`ObjI::Run` ×2 |

### 钩子 F：角色怎么调用到别的 itemSpt 里的 item

游戏把道具包写死在 `World.AddObjI` 里（`Global.sptHash["item"]`），而 `cre` 只传一个
组号过去。钩子 F 加的就是"去哪个包取"这个信息：

    Frame.cre[].itemSptRoute                 <- 移植时写进 SPT 载荷
      ObjH::Run / ObjI::Run 生成道具前：world.hfpRoute = this.cre.itemSptRoute
        World.AddObjI：读进局部变量并立刻清空，用 Global.ResolveItemSpt(route) 取包，
                       再把 route 盖到新建的 ObjI 上
          World.ReboundBlast：this.hfpRoute = param1.itemSptRoute
                              （反弹爆炸要用道具自己那个包里的 agi）

`ResolveItemSpt(null)` 返回目标默认包，所以剧情道具、地图 dummy、硬编码
`iteHash["fire_explode"]` 等一切没设 route 的调用点行为完全不变。

`Data.Pt` 是 sealed 类（flags 0x09 含 ClassSealed），AMF0 反序列化只能写**已声明**的
属性，所以 `Pt.itemSptRoute` 这个槽位不是可选的：不声明，载荷里带这个字段会在
`LoadAllData` 里抛异常，而那里有 `catch(Error)` —— 于是静默中止后面所有加载。
新槽位类型一律用 `*`，标一个解析不到的类型同样会让整个类被 AVM2 verifier 拒掉。

**已知取舍**：`itemSptRoute` 是 HFWorkshop 不认识的字段。如果把移植后的角色 SPT
从 HFWorkshop 重新导出，这个字段会被丢掉，那些 cre 会静默退回目标默认 item 包。

### 原有角色 / item 调用移植贴图

新增的角色 LMI 和独立 item LMI 都会提前加载，**原有角色、原有 item、新移植角色
可以按名字引用本次新增的贴图**，不再局限于移植角色自己使用。

原因是 `Data.Spt.LoadFromBytes` 在读取时，就把 `spriteLimb.limbName` 和换色表
`limbNames[a..f]` 一次性绑定成 `Global.limb[name]`。如果先读 SPT、后读 LMI，
当时的空引用不会自动重绑，结果是空气人 / 无贴图，且不报错。

现在的加载顺序（阶段号以 v0.7 系为例）：

    钩子 A  LoadGlobalDatFromCompressedBytes  <- 本次全部新增 LMI（角色 + item）
    +95     lmi_data/item.lmi                 <- 目标原有贴图
    +201 起 原有角色 SPT                      <- 此时能找到新增 limb
    +307    spt_data/item.spt                 <- 目标原有 item，同样能找到
    +501    bgi_data/bg0.bgi / 钩子 B          <- 新增 SPT，保持 refSpt 依赖顺序

过去全部新资源都放在 B，只保证了新移植角色自身能用，原有角色和原有 item 已经错过
绑定时机。`verify.py` 现在从产物字节码核对完整的加载调用：每个新增 LMI 在 A 恰好
一次、每个新增 SPT 在 B 恰好一次，并检查顺序；不再把路径字符串存在当作加载成功。

在 HFWorkshop 中引用时：

- 写 **LMI 内部的最终 `Limb.name`** 到 SPT 的 `spriteLimb.limbName`（或换色表
  `limbNames`），不是 EXE 资源类名或 `.lmi` 文件名。工具改名后的 `@命名空间` 后缀
  也是名字的一部分，按输出 LMI 中的实际名字填写。
- 对应帧的贴图槽位也必须在这个 limb 中存在。不同 limb 的槽位不一定对应同一部位，
  提前加载不会自动转换姿势或槽位。
- 仅改 `lmiFiles` 不会建立运行时绑定，它只是编辑器的文件路径列表。
- **贴图共享不等于 item 动作包共享**；生成独立包里的 item 仍遵循上面的路由规则。
- 此修复针对**以后由新版工具新增的资源**，更新 Python 工具不会自动修改已经生成的
  EXE，也不会迁移目标 EXE 中旧移植批次留在 B 的加载调用。

### 构建前的容器自检

读入目标后，工具会在补丁全关的状态下重建一遍目标 SWF，并和**规范化后的原始字节**
比对（压缩容器 CWS/ZWS 先解压成 FWS 再比）。结果只有三种：逐字节一致、
仅容器压缩形式不同（标签内容完全一致，只是重建为未压缩 FWS，文件会变大）、
或者内容不一致——最后一种直接拒绝构建，因为那意味着无法保证不破坏目标。
这样"输出里和目标不同的每一个字节都是这次移植改的"是可验证的结论。

## 已知边界

- **引擎代码不移植**：若源 mod 的某机制是改 AS3 引擎实现的（不是数据），
  移植后按目标 mod 引擎表现。
- 源角色若缺少 pow、SPT 或合法经验绑定，工具会在预检阶段停止并列出缺失项，
  不会擅自回退到 Lucas。
- **BGM 不移植**（角色实际用到的差异战斗音效会作为新增音效自动移植）。
- 雇佣兵用目标版本现有的同名士兵。
- 联机对战需双方使用同一个移植后的 EXE。
- 加载画面会增加读条时间（新增贴图在前段加载，新增 SPT 在末段加载）。

## 目录结构

    hfport/          工具本体（amf/swf/abcpatch/gamedata/porter/loaders/itemroute/registry/builder/repair/verify/cli）
    hfport/tests/    集成自检脚本（需要本地游戏文件和 FFDec；公开仓库不提供）
    移植工具.bat      双击入口
    output/          移植产物与配置
    work/            中间文件（自动重建，已由 Git 忽略）

`work/registry/` 下的 FFDec 注册清单按输入 EXE 的 SHA-256 分目录缓存，不同版本
不会再误用之前运行留下的 `reg_src/reg_dst`。
