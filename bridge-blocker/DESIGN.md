# 游戏设计文档

## 一句话定位

俯视 2D 守桥防御游戏。玩家是桥南守军，独自带一个 AI 队友，抵挡从桥右源源不断刷新的敌方步兵+狙击手。

## 流程

1. 打开网页 → 加载 5 张精灵 → 进入**开始菜单**（黑幕 + 标题 + 操作提示 + 绿色开始按钮）
2. 点击开始按钮 → `startGame()` 锚定 `startedAt` 与所有计时器
3. 进入核心循环（见下）
4. 任意一方倒下 → GAME OVER → 按 R 刷新回到开始菜单

## 核心循环

1. 敌人从画面右侧涌入，走向三个固定掩体位
2. 到位后开始**上下晃动 + 周期开枪**
3. 玩家鼠标点击瞄准射击；队友 AI 自动协助
4. 敌人 HP 归 0 → 倒下→ 5 秒淡出
5. 每 7 秒检查刷新，每位最多 N 个（默认 2）
6. 玩家或队友 HP 归 0 → GAME OVER

## 角色与武器

### 玩家：乌鲁鲁（绿队大胡子老兵）
- HP: 200
- 位置: 桥南固定（暂未实现移动）
- 武器: 黄色子弹，每 150ms 冷却
- 伤害: 头 52.5 / 身 35
- 爆头判定: 命中点 Y ≤ 头部判定线（默认占敌人身高顶部 30%）

### 队友：威龙（黄队装甲兵）
- HP: 200
- 位置: 桥北固定
- 武器: 蓝色子弹（更快，14 px/帧）
- AI: 每 2 秒开一枪，50% 概率命中（命中即强制爆头 52.5），50% 空枪打偏
- 命中目标: 随机锁定一个活着的敌人
- 玩家无法误伤队友

### 奶妈：蜂医
- 位置: 桥中央，乌鲁鲁与威龙的中点
- HP: 当前简化为不可被打（敌人 AI 选目标时只考虑玩家+威龙），代码里 hp=1 仅作存活标记
- 治疗 AI: 每 10 秒给乌鲁鲁和威龙**同时**各 +25 HP，超过血量上限不溢出
- 治疗视觉: 绿色水枪光束（线性渐变 + 末端水花）+ 头顶绿字 +N
- 主动技能·烟雾:
  - **触发**: 玩家点击蜂医身上（精灵框命中检测）
  - **冷却**: 40 秒（释放后 40 秒才能再放，含持续期）
  - **持续**: 20 秒
  - **效果**: 烟雾期间敌人命中分布替换为 0% 爆头 / 25% 身体 / 75% 空枪
  - **视觉**: 30 团灰色烟雾随机分布在桥右敌人区，单团生命周期内淡入→稳定→淡出
  - **HUD 状态**: 顶栏正中显示"就绪 / 烟雾中 Ns / 冷却 Ns"

### 敌方·步枪手
- HP: 100
- AI: 到位后每 ~3 秒开一枪
- 命中分布: 25% 爆头 / 25% 身体 / 50% 空枪
- 伤害: 头 50 / 身 25
- 子弹: 红色，速度 8 px/帧

### 敌方·狙击手
- HP: 100
- AI 同步枪手节奏
- 命中分布同步枪手
- 伤害: 头 **100** / 身 **50**（双倍）
- 标记: 头顶 🎯 狙击手

### 敌方·配比
- 每生成 3 个敌人 → 1 个狙击手 + 2 个步枪手
- 由全局计数器 `enemyCounter % 3 === 0` 决定为狙击手
- 初始三个埋伏（位置 0/1/2）：第 0 个是狙击手

## 战场布局（坐标系）

画面 W=640, H=800。

```
y=0     ────────────────────  顶栏 HUD
y=200   ━━━━━━━━━━━━━━━━━━━━  马路上沿
        ┃                  
        桥                    │ 桥栏杆
        BRIDGE_X=120         │
        BRIDGE_WIDTH=160     │
y=300                         三个掩体（80×80）
y=400   - - - - 黄线 - - - -    呈三角阵
y=500                         
        ┃
y=600   ━━━━━━━━━━━━━━━━━━━━  马路下沿
y=800
```

掩体三角阵参数（drawScene + spawn 共用）：
- `coverSize = 80` 每块大小
- `coverGap  = 80` 块之间缝
- `offsetX   = 40` 整组离桥右栏多远

三个终点位（敌人到位后的锚点）：
- 0: 前排掩体右（最靠桥）
- 1: 后排上方掩体右
- 2: 后排下方掩体右

## 视觉效果

- **枪口闪光** muzzle: 双层圆形，90ms 淡出
- **命中火花** spark: 6 条放射线 200ms 淡出
- **伤害飘字** damage: 命中点向上漂 30px，700ms 淡出。爆头红色加大字号"暴击 N"，身体黄色"N"
- **命中红光罩** hitFlash: 敌人身上 120ms 红色半透明覆盖
- **死亡淡出** deadAt: 5 秒线性 alpha 从 0.35 → 0
- **治疗光束** healBeam: 蜂医 → 目标的绿色渐变粗线 + 末端粒子，600ms 淡出
- **烟雾团** smoke: 30 团灰色圆，淡入 8% / 稳定 / 淡出 15%，整体 20 秒

## 调节面板（HTML 控件）

| 控件 | 变量 | 范围 | 说明 |
|---|---|---|---|
| 爆头范围滑杆/数字 | `HEAD_RATIO` | 0.00–1.00 | 头部占身高比例 |
| 显示爆头区 | `SHOW_HEAD_ZONE` | bool | 在敌人身上画红框 |
| 每位最多几个 | `MAX_PER_ANCHOR` | 1–20 | 每个终点位的容量 |

## 紧密包围盒（重要细节）

PNG 贴图周围有透明像素。所有命中判定与红框绘制都用**像素扫描得到的紧密 bbox**，不是图片的 naturalWidth/Height。
- `computeTightBounds(img)` 在图片加载时扫一次，缓存到 `img._tight = {topR, botR, leftR, rightR}`
- `enemyBox(e)` 和 `friendlyBox(target)` 都基于 tight bounds

## 主要可调常量（顶部 ✨）

```js
// 画面
W, H, ROAD_COLOR, GRASS_COLOR, BRIDGE_COLOR, BRIDGE_WIDTH, BRIDGE_X

// 战士
SOLDIER_HEIGHT, ENEMY_HEIGHT
PLAYER_NAME, ENEMY_NAME

// 武器
BULLET_SPEED, BULLET_RADIUS, FIRE_COOLDOWN, BULLET_COLOR

// HP / 伤害
ENEMY_MAX_HP, HEAD_DAMAGE, BODY_DAMAGE, HEAD_RATIO
PLAYER_MAX_HP, ALLY_MAX_HP

// 蜂医
MEDIC_NAME, MEDIC_HEIGHT
HEAL_INTERVAL_MS, HEAL_AMOUNT
SMOKE_COOLDOWN_MS, SMOKE_DURATION_MS
SMOKE_HIT_HEAD_PROB, SMOKE_HIT_BODY_PROB

// 刷新
SPAWN_INTERVAL_MS, ENEMY_WALK_SPEED, IDLE_RANGE, IDLE_SPEED, MAX_PER_ANCHOR

// 队友
ALLY_FIRE_INTERVAL_MS, ALLY_HIT_RATE, ALLY_BULLET_SPEED, ALLY_BULLET_COLOR

// 敌方射击
ENEMY_FIRE_INTERVAL_MS, ENEMY_BULLET_SPEED, ENEMY_BULLET_COLOR
ENEMY_HIT_BODY_PROB, ENEMY_HIT_HEAD_PROB
RIFLE_BODY_DMG, RIFLE_HEAD_DMG, SNIPER_BODY_DMG, SNIPER_HEAD_DMG

// 死亡
DEATH_FADE_MS
```

## 代码结构（index.html 内）

```
<style>          基本布局 + 调节面板样式
<canvas>         640x800 主画布
<div class=panel> 调节面板 HTML

<script>
  常量参数区
  drawScene()     画地面、桥、掩体（每帧重画）
  base64 贴图     SOLDIER_*_DATA, ENEMY_*_DATA
  loadImage / computeTightBounds
  drawSoldier()
  game = { player, enemies, bullets, effects, ... }
  enemyBox / friendlyBox
  spawnBullet (玩家点击)
  allyFire (队友 AI)
  enemyFire (敌方 AI)
  medicHeal (蜂医每 10s 治疗)
  tryActivateSmoke / isSmokeActive (烟雾技能)
  spawnEnemyWave (每 7 秒)
  updateEnemies / updateBullets
  drawBullets / drawHpBar / drawHitFlash / drawHeadZone / drawSniperMark / drawEffects / drawHUD / drawGameOver
  render() / loop()
  鼠标点击 / 键盘 R 重启
  Promise.all(loadImage * 5).then(初始化游戏)
</script>
```
