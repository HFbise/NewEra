-- =====================================================
-- AI MUD 数据库结构（Supabase / Postgres）
-- 分两层：
--   静态内容：房间、物品模板、NPC 模板（世界设计，很少变）
--   动态状态：玩家、NPC 实例、物品实例、事件日志（游戏中不断变化）
-- =====================================================

-- ---------- 静态内容 ----------

create table rooms (
  id          text primary key,
  name        text not null,
  description text not null,                     -- 玩家看到的简短描述
  details     text not null default '',          -- 只给 AI 的环境细节，不能拿、不参与规则，freeform 可以用
  props       jsonb not null default '{}'        -- forage（搜索能找到的东西）、dispensers（武器桶这类取用处），见 world.yaml
);

create table item_templates (
  id          text primary key,
  name        text not null,
  description text not null,
  type        text not null check (type in ('weapon','armor','consumable','key','misc')),
  takeable    boolean not null default true,
  stackable   boolean not null default false,   -- 面包、药草这类可以叠加
  damage      int not null default 0,
  defense     int not null default 0,
  heal        int not null default 0,
  slot        text check (slot in ('hand','ring','head','chest','belt','legs','feet','neck')),  -- 能装在哪，见 schema.Slot
  props       jsonb not null default '{}'       -- 以后加新属性先放这里，稳定了再升成独立列
);

create table room_exits (
  room_id     text not null references rooms(id) on delete cascade,
  direction   text not null,
  to_room     text not null references rooms(id) on delete cascade,
  locked      boolean not null default false,   -- 当前是否上锁（会被玩家改变）
  key_item    text references item_templates(id),
  relock_seconds int,                            -- 打开后多久自动锁回去，为空就不自动锁
  unlocked_at timestamptz,
  primary key (room_id, direction)
);

create table npc_templates (
  id          text primary key,
  name        text not null,
  description text not null,
  persona     text not null,
  hostile     boolean not null default false,
  max_hp      int,                               -- 为空表示不可战斗
  attack      int not null default 0,
  defense     int not null default 0,
  props       jsonb not null default '{}'
);

-- ---------- 动态状态 ----------

create table players (
  id          uuid primary key references auth.users(id) on delete cascade,
  name        text not null unique,
  room_id     text not null references rooms(id),
  hp          int not null,
  max_hp      int not null,
  attack      int not null,
  defense     int not null,
  flags       jsonb not null default '{}',       -- 任务标记，如 {"goblin_slain": true}
  last_active_at timestamptz,                    -- 最近一次操作或页面心跳，太久没动就算睡着
  password_hash  text,                           -- 开发期的角色密码（scrypt），正式版换 Supabase Auth
  party_id    uuid,                              -- 所在队伍，同一队的人这个值相同；没组队为空
  status      jsonb,                             -- 负面状态 {kind, label, escape, attempts, since}，见 schema.Status
  following   uuid references players(id) on delete set null,  -- 正在跟着的玩家，对方移动时一起走
  stealth     jsonb,                             -- 在有敌人的区域里有没有被发现（schema.Stealth），换区域作废
  skills      jsonb not null default '{}',       -- 生活技能的熟练次数 {技能: 次数}，等级由次数算（engine.skill_level）
  drunk_until timestamptz,                       -- 喝醉到什么时候（engine.DRUNK_TIME）
  drinks      int not null default 0,            -- 连着喝了几杯，喝醉几率随它指数上升
  last_drink_at timestamptz,
  downed_by   jsonb,                             -- 上次倒下的原因 {kind: npc|player|poison, by}，看店 NPC 扶人时说俏皮话
  gold        int not null default 0 check (gold >= 0),       -- 金币：打怪掉，跟 NPC 买东西花
  deepest_floor int not null default 0,          -- 到过远古地牢最深第几层（地窖石碑排行榜）
  waypoints   int[] not null default '{}',       -- 到过的传送石在第几层，能从地窖直接传送过去
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- NPC 实例：同一个模板可以刷出多只（三只哥布林）
create table npcs (
  id          uuid primary key default gen_random_uuid(),
  template_id text not null references npc_templates(id),
  room_id     text references rooms(id),
  hp          int,
  alive       boolean not null default true,
  died_at     timestamptz,                       -- 死亡时间，模板 props.respawn_seconds 之后复活
  memory      text not null default '',         -- 长期记忆摘要
  status      jsonb                              -- 负面状态，同 players.status
);

-- 可利用地形：能拿来砸人、绊人的环境物件。用掉 uses 次后 respawn_seconds 秒恢复
-- details 里没列在这的东西也能用，但最多轻伤（engine.IMPROVISED_MAX_TIER）
create table room_features (
  id              uuid primary key default gen_random_uuid(),
  room_id         text not null references rooms(id) on delete cascade,
  key             text not null,                 -- world.yaml 里的名字
  name            text not null,
  max_tier        text not null check (max_tier in ('light','heavy','lethal')),
  max_uses        int not null default 1,
  uses_left       int not null,
  respawn_seconds int not null default 300,
  used_at         timestamptz,                   -- 第一次被用掉的时间，到点补满
  unique (room_id, key)
);

-- 物品实例：每一件实际存在的东西，位置三选一（房间 / 玩家 / NPC）
create table item_instances (
  id            uuid primary key default gen_random_uuid(),
  template_id   text not null references item_templates(id),
  quantity      int not null default 1 check (quantity > 0),
  room_id       text references rooms(id) on delete cascade,
  player_id     uuid references players(id) on delete cascade,
  npc_id        uuid references npcs(id) on delete cascade,
  equipped_slot text check (equipped_slot in ('head','chest','belt','legs','feet','neck','ring1','ring2','left_hand','right_hand')),
  props         jsonb not null default '{}',     -- 实例级别的变化，如耐久度、附魔
  check (num_nonnulls(room_id, player_id, npc_id) = 1),
  check (equipped_slot is null or player_id is not null)
);

-- 每个玩家每个装备槽只能有一件
create unique index one_item_per_slot
  on item_instances (player_id, equipped_slot)
  where equipped_slot is not null;

create index on item_instances (room_id)   where room_id   is not null;
create index on item_instances (player_id) where player_id is not null;
create index on item_instances (npc_id)    where npc_id    is not null;
create index on npcs (room_id) where alive;

-- 玩家和 NPC 的关系：按 NPC 模板记，seed 重置 NPC 实例也不会丢
create table player_npc_relations (
  player_id    uuid not null references players(id) on delete cascade,
  npc_template text not null references npc_templates(id) on delete cascade,
  affinity     int not null default 0 check (affinity between -100 and 100),  -- 好感度
  memory       text not null default '',        -- NPC 对这个玩家的记忆摘要，对话时由 AI 更新，限 150 字
  last_created_at timestamptz,                   -- NPC 上次给这个玩家现造东西的时间，冷却用（engine.CREATE_COOLDOWN）
  offers       jsonb not null default '{}',     -- NPC 给这个玩家报过的价 {物品 id: {price, at}}，卖货只按报价成交
  primary key (player_id, npc_template)
);

create index on players (party_id) where party_id is not null;

-- 组队邀请：被邀请的人回"加入某某的队伍"才入队，engine.INVITE_WINDOW 内有效
create table party_invites (
  inviter     uuid not null references players(id) on delete cascade,
  invitee     uuid not null references players(id) on delete cascade,
  created_at  timestamptz not null default now(),
  primary key (inviter, invitee)
);

-- 事件日志：给 AI 提供最近发生的事，也用来给前端推送
create table events (
  id          bigserial primary key,
  room_id     text not null references rooms(id),
  player_id   uuid references players(id) on delete set null,
  kind        text not null,                     -- move / attack / talk / freeform ...
  facts       jsonb not null default '[]',       -- 规则引擎输出的客观事实
  narrative   text,                              -- AI 生成的叙事（给行动者本人，第二人称）
  observer    text,                              -- 给同房间其他人看的第三人称描述
  input       text,                              -- 玩家输入原话（后台排查用）
  meta        jsonb,                             -- 解析来源、解析出的动作、提示（后台排查用）
  created_at  timestamptz not null default now()
);

create index on events (room_id, created_at desc);
create index on events (room_id, id);

-- 任务：world.yaml 的 quests 段，seed/sync 导入。NPC 按玩家的进度主动提起、提醒、发奖励
create table quests (
  id          text primary key,
  giver       text not null references npc_templates(id) on delete cascade,  -- 发布任务的 NPC 模板
  name        text not null,
  hook        text not null,                     -- 给 AI：NPC 怎么提起这件事
  goal        text not null,                     -- 要玩家做什么
  done_flag   text,                              -- 玩家有这个标记就算完成
  needs_item  text references item_templates(id),  -- 或者：玩家带着这件东西来找发布者就算完成（东西会被收走）
  hidden      boolean not null default false,    -- 隐藏委托：NPC 不主动提，玩家自己碰上才触发
  reward_item text references item_templates(id),  -- 完成后跟发布者说话自动给的东西
  after       text not null default ''           -- 了结后给 AI 的一句话
);

-- 玩家的任务进度。没有记录 = 还没接；offered = NPC 提过了；rewarded = 奖励给了，了结。
-- 做没做完不存，看玩家的 flags 里有没有 done_flag
create table player_quests (
  player_id   uuid not null references players(id) on delete cascade,
  quest_id    text not null references quests(id) on delete cascade,
  status      text not null check (status in ('offered', 'rewarded')),
  updated_at  timestamptz not null default now(),
  primary key (player_id, quest_id)
);

-- 每个玩家的聊天框记录：刷新页面、重新登录后拉回来显示
-- turn = 自己的一回合（输入、解析、facts、叙事），event = 看到的别人的动态（同一条 event 只记一次）
create table player_log (
  id          bigserial primary key,
  player_id   uuid not null references players(id) on delete cascade,
  kind        text not null check (kind in ('turn', 'event')),
  event_id    bigint,
  data        jsonb not null,
  created_at  timestamptz not null default now()
);

create index on player_log (player_id, id);
create unique index on player_log (player_id, event_id) where event_id is not null;

-- 刷新点：物品被拿走后过一段时间重新出现。位置二选一：房间地上 / 某种 NPC 身上
-- 不用后台任务，有人在这个房间时顺手检查（engine._refresh_room）
create table spawns (
  id              serial primary key,
  room_id         text references rooms(id) on delete cascade,
  npc_template    text references npc_templates(id) on delete cascade,
  template_id     text not null references item_templates(id) on delete cascade,
  respawn_seconds int not null,
  empty_since     timestamptz,                   -- 发现东西不在了的时间
  check (num_nonnulls(room_id, npc_template) = 1)
);

-- NPC 对每个玩家的完整来往记录（他说了什么、NPC 回了什么、结果如何），全部留着；
-- player_npc_relations.memory 是 AI 写的长期总结，给 AI 看的是总结 + 最近几条
create table npc_memory_log (
  id           bigserial primary key,
  player_id    uuid not null references players(id) on delete cascade,
  npc_template text not null references npc_templates(id) on delete cascade,
  entry        text not null,
  created_at   timestamptz not null default now()
);
create index on npc_memory_log (player_id, npc_template, id);

-- NPC 现做过的东西（按名字）：下次有人要同一样就照原规格做，价钱参考上次
create table npc_goods (
  npc_template text not null references npc_templates(id) on delete cascade,
  name         text not null,
  spec         jsonb not null,                   -- 同 engine.made_spec：kind、description、heal、harm、damage、knockout
  price        int,                              -- 上次开的价，白送的不记
  updated_at   timestamptz not null default now(),
  primary key (npc_template, name)
);

-- 玩家决斗（PvP）：没接受前是申请，接受了才能互相造成伤害。每人同时只发起一场。
-- 双方不在 room_id 这里了、有人倒下了就算结束（engine._end_stale_duels 清掉）
create table duels (
  challenger  uuid primary key references players(id) on delete cascade,
  target      uuid not null references players(id) on delete cascade,
  room_id     text not null references rooms(id) on delete cascade,
  accepted    boolean not null default false,
  distance    int not null default 3,
  dodging     uuid[] not null default '{}',     -- 摆好了闪避的一方，对手下一次攻击时生效
  created_at  timestamptz not null default now()
);
create index on duels (target);

-- 取用处拿过的记录：配了 once 的（地窖桌上的护符）拿过一次就再也不能拿，丢了也一样
create table dispenser_log (
  player_id   uuid not null references players(id) on delete cascade,
  room_id     text not null references rooms(id) on delete cascade,
  key         text not null,
  taken_at    timestamptz not null default now(),
  primary key (player_id, room_id, key)
);

-- 搜索找到东西的记录：每个人各算各的冷却，不再先到先得
create table forage_log (
  player_id   uuid not null references players(id) on delete cascade,
  room_id     text not null references rooms(id) on delete cascade,
  template_id text not null references item_templates(id) on delete cascade,
  found_at    timestamptz not null default now(),
  primary key (player_id, room_id, template_id)
);

-- AI 调用记录：每次调用的 token 数，用来算成本
create table ai_calls (
  id              bigserial primary key,
  player_id       uuid references players(id) on delete set null,
  kind            text not null,                 -- intent / narrate
  model           text not null,
  input_tokens    int not null,
  output_tokens   int not null,
  cache_read      int not null default 0,
  cache_write     int not null default 0,
  latency_ms      int not null,
  ok              boolean not null,              -- 输出是否通过校验
  error           text,                          -- 调用本身报错时的错误类型（限流、超时），这时 token 为 0
  created_at      timestamptz not null default now()
);

create index on ai_calls (created_at desc);

-- ---------- 权限 ----------
-- 所有写操作只走服务器（service_role 绕过 RLS），客户端只读
alter table rooms          enable row level security;
alter table room_exits     enable row level security;
-- 远古地牢：每支队伍（没组队就是自己）一份，按需一层层生成（dungeon.py），房间 id 以 dg-<run>- 开头
create table dungeon_runs (
  id             uuid primary key default gen_random_uuid(),
  owner          uuid references players(id) on delete set null,
  party_id       uuid,                           -- 组队进的就是队伍的，队友进来走同一份
  created_at     timestamptz not null default now(),
  last_active_at timestamptz not null default now()   -- 没人在里面超过 dungeon.STALE 就整份删掉
);

create table dungeon_floors (
  run_id      uuid not null references dungeon_runs(id) on delete cascade,
  depth       int not null,
  theme       text not null,                     -- dungeon.yaml 的主题
  entry_room  text not null,
  stairs_room text not null,
  party_size  int not null default 1,           -- 生成时队伍在线人数：怪的血量、钱袋跟着涨
  camped      uuid[] not null default '{}',     -- 在这一层扎过营的人（每层每人一次）
  primary key (run_id, depth)
);


alter table item_templates enable row level security;
alter table npc_templates  enable row level security;
alter table players        enable row level security;
alter table npcs           enable row level security;
alter table item_instances enable row level security;
alter table events         enable row level security;
alter table player_npc_relations enable row level security;
alter table ai_calls       enable row level security;   -- 不开放给客户端
alter table spawns         enable row level security;
alter table party_invites  enable row level security;
alter table room_features  enable row level security;
alter table player_log     enable row level security;
alter table quests         enable row level security;
alter table player_quests  enable row level security;
alter table forage_log     enable row level security;
alter table dispenser_log  enable row level security;
alter table npc_memory_log enable row level security;
alter table duels          enable row level security;
alter table npc_goods      enable row level security;
alter table dungeon_runs   enable row level security;
alter table dungeon_floors enable row level security;

create policy "read static" on rooms          for select to authenticated using (true);
create policy "read static" on room_exits     for select to authenticated using (true);
create policy "read static" on item_templates for select to authenticated using (true);
create policy "read static" on npc_templates  for select to authenticated using (true);
create policy "read own"    on players        for select to authenticated using (id = auth.uid());
create policy "read own"    on item_instances for select to authenticated using (player_id = auth.uid());
create policy "read own"    on player_npc_relations for select to authenticated using (player_id = auth.uid());
-- 只能看到自己当前所在房间的事件
create policy "read room"   on events         for select to authenticated
  using (room_id = (select room_id from players where id = auth.uid()));
