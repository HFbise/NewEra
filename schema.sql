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
  details     text not null default ''           -- 只给 AI 的环境细节，不能拿、不参与规则，freeform 可以用
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
  props       jsonb not null default '{}'       -- 以后加新属性先放这里，稳定了再升成独立列
);

create table room_exits (
  room_id     text not null references rooms(id) on delete cascade,
  direction   text not null,
  to_room     text not null references rooms(id) on delete cascade,
  locked      boolean not null default false,   -- 当前是否上锁（会被玩家改变）
  key_item    text references item_templates(id),
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
  memory      text not null default ''          -- 长期记忆摘要
);

-- 物品实例：每一件实际存在的东西，位置三选一（房间 / 玩家 / NPC）
create table item_instances (
  id            uuid primary key default gen_random_uuid(),
  template_id   text not null references item_templates(id),
  quantity      int not null default 1 check (quantity > 0),
  room_id       text references rooms(id) on delete cascade,
  player_id     uuid references players(id) on delete cascade,
  npc_id        uuid references npcs(id) on delete cascade,
  equipped_slot text check (equipped_slot in ('weapon','armor')),
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
  primary key (player_id, npc_template)
);

-- 事件日志：给 AI 提供最近发生的事，也用来给前端推送
create table events (
  id          bigserial primary key,
  room_id     text not null references rooms(id),
  player_id   uuid references players(id) on delete set null,
  kind        text not null,                     -- move / attack / talk / freeform ...
  facts       jsonb not null default '[]',       -- 规则引擎输出的客观事实
  narrative   text,                              -- AI 生成的叙事
  created_at  timestamptz not null default now()
);

create index on events (room_id, created_at desc);

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
  created_at      timestamptz not null default now()
);

create index on ai_calls (created_at desc);

-- ---------- 权限 ----------
-- 所有写操作只走服务器（service_role 绕过 RLS），客户端只读
alter table rooms          enable row level security;
alter table room_exits     enable row level security;
alter table item_templates enable row level security;
alter table npc_templates  enable row level security;
alter table players        enable row level security;
alter table npcs           enable row level security;
alter table item_instances enable row level security;
alter table events         enable row level security;
alter table player_npc_relations enable row level security;
alter table ai_calls       enable row level security;   -- 不开放给客户端

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
