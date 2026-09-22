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
  description text not null
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

create policy "read static" on rooms          for select to authenticated using (true);
create policy "read static" on room_exits     for select to authenticated using (true);
create policy "read static" on item_templates for select to authenticated using (true);
create policy "read static" on npc_templates  for select to authenticated using (true);
create policy "read own"    on players        for select to authenticated using (id = auth.uid());
create policy "read own"    on item_instances for select to authenticated using (player_id = auth.uid());
-- 只能看到自己当前所在房间的事件
create policy "read room"   on events         for select to authenticated
  using (room_id = (select room_id from players where id = auth.uid()));
