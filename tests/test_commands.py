"""规则解析器：常见说法直接变成动作；拿不准的整句交给 AI（freeform）"""
import uuid

import pytest

import commands
from schema import ItemInstance, ItemTemplate, Npc, NpcTemplate, Player, Room, RoomExit, RoomView


@pytest.fixture
def view() -> RoomView:
    v = RoomView(
        player=Player(id=uuid.uuid4(), name="阿青", room_id="tavern", hp=100, max_hp=100, attack=20, defense=0),
        room=Room(id="tavern", name="酒馆", description=""),
        exits=[RoomExit(room_id="tavern", direction="north", to_room="square")],
        items=[],
        npcs=[Npc(id=uuid.uuid4(), template=NpcTemplate(id="goblin", name="哥布林", description="", persona="", hostile=True,
                                                         max_hp=80), room_id="tavern", hp=80),
              Npc(id=uuid.uuid4(), template=NpcTemplate(id="innkeeper", name="麦琪", description="", persona=""), room_id="tavern")],
        inventory=[ItemInstance(id=uuid.uuid4(), template=ItemTemplate(id="blood_potion", name="血药", description="",
                                                                       type="consumable"))],
    )
    v.assign_refs()                 # 哥布林 n1、麦琪 n2、血药 i1
    return v


def one(view, text):
    acts = commands.parse(view, text)
    assert len(acts) == 1
    return acts[0].model_dump(exclude_none=True)


@pytest.mark.parametrize("text", ["往北走", "北", "向北"])
def test_move(view, text):
    assert one(view, text) == {"action": "move", "direction": "north"}


def test_attack_resolves_the_target_to_a_ref(view):
    assert one(view, "攻击哥布林") == {"action": "attack", "target": "n1"}


def test_use_item_by_name(view):
    assert one(view, "喝血药") == {"action": "use", "item": "i1"}


def test_pay_with_chinese_numbers(view):
    assert one(view, "给麦琪两金币") == {"action": "pay", "target": "n2", "amount": 2}


def test_say_keeps_the_message(view):
    assert one(view, "说 大家好") == {"action": "say", "message": "大家好"}


@pytest.mark.parametrize("text,action", [("闭上眼睛", "close_eyes"), ("背过身去", "close_eyes"), ("掐自己", "pinch"),
                                         ("躲起来", "hide"), ("挣脱", "struggle"), ("站起来", "stand"), ("逃跑", "flee"),
                                         ("看看四周", "look")])
def test_simple_actions(view, text, action):
    assert one(view, text)["action"] == action


def test_unsure_phrasings_go_to_the_ai(view):
    # 规则拿不准的不硬猜，整句交给 AI 解析
    assert one(view, "跳一段舞") == {"action": "freeform", "description": "跳一段舞"}


def test_semicolons_split_a_turn_into_several_actions(view):
    acts = commands.parse(view, "往北走；看看四周")
    assert [a.action for a in acts] == ["move", "look"]
