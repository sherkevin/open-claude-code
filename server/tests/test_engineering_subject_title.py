"""Independent public-topic checks. These fixtures contain synthetic text only."""
import pytest

from app.conversation_title import DEFAULT_TITLE, first_message_title
from test_api import FakeRuntime, PASSWORD, conversation, login
from test_codex_appserver_host import log_events, requests
from test_conversation_codex import codex_env
from test_engineering_conversation_topic import BASE, home_title, turn


CASES = {
    ("订单", "导出"): "麻烦帮我排查一下，订单导出点了以后一直没有反应。",
    ("订单", "预览"): "麻烦帮我排查一下，订单预览打开后一直没显示内容。",
    ("发票", "导出"): "麻烦帮我排查一下，发票导出点了以后一直没有反应。",
    ("发票", "预览"): "麻烦帮我排查一下，发票预览打开后一直没显示内容。",
}


def topic(text):
    return first_message_title(text, engineering=True)


def assert_recognizable(label, subject, action):
    # Labels can choose their own wording, punctuation, and general topic.
    equivalent = {"导出": ("导出", "下载"), "预览": ("预览", "查看")}
    assert subject in label, label
    assert any(word in label for word in equivalent[action]), label


def test_actions_and_objects_each_distinguish_two_similar_home_rows():
    titles = {pair: topic(text) for pair, text in CASES.items()}
    assert len(set(titles.values())) == len(CASES), titles
    for (subject, action), title in titles.items():
        assert_recognizable(title, subject, action)


@pytest.mark.parametrize(("subject", "action", "prompt"), [
    ("发票", "预览", "我打开一张发票，预览页是空白的。帮我看看原因。"),
    ("订单", "导出", "请排查为什么导出订单时卡住了。"),
])
def test_recognizable_subject_survives_reordering_and_conversational_filler(subject, action, prompt):
    assert_recognizable(topic(prompt), subject, action)


def test_customer_secret_and_paths_are_not_published_with_the_topic():
    personal = ("山雀测试客户", "SYNTHETIC_ONLY_CANARY_761", "/private/customer-761/exports.json",
                r"C:\private\customer-761\exports.json", "未公开批号水杉谷")
    prompt = "订单导出不响应，请排查。" + "。".join(personal)
    title = topic(prompt)
    assert_recognizable(title, "订单", "导出")
    assert all(part not in title for part in (*personal, "customer-761", "水杉谷", "761", prompt))
    # Unrecognized prose has no authority to supply arbitrary labels.
    unknown = [topic("请解释" + name) for name in ("禾木雾舟甲", "石苇松舟乙")]
    assert all(name not in title for name, title in zip(("禾木雾舟甲", "石苇松舟乙"), unknown))
    assert unknown[0] == unknown[1]


def test_other_topics_remain_recognizable_without_enabling_engineering_for_regular_chat():
    assert topic("请解释 pwd 如何显示当前工作目录") != topic("date 命令怎样显示今天的日期")
    assert topic("Git 分支如何切换") != topic("我想在 Git 中查出是哪一次提交引入了回归")
    assert topic("怎样安装依赖") != topic("如何运行测试")
    assert first_message_title("查询剩余库存") == "库存查询"


def test_owner_home_displays_subject_and_action_without_extra_native_turns(codex_env, tmp_path):
    client, _, _ = codex_env
    before_runtime = FakeRuntime.connects
    cases = [(subject, action, text, conversation(client)["id"]) for (subject, action), text in CASES.items()]
    for index, (subject, action, text, cid) in enumerate(cases):
        result = turn(client, cid, f"public-topic-{index}", text)
        assert result.status_code == 200
    titles = [home_title(client, cid) for _, _, _, cid in cases]
    assert len(set(titles)) == len(cases), titles
    for (subject, action, _, _), title in zip(cases, titles):
        assert_recognizable(title, subject, action)
    _, _, original, first = cases[0]
    replay = turn(client, first, "public-topic-0", original)
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert home_title(client, first) == titles[0]
    assert sum(len(requests(log_events(log), "turn/start")) for log in tmp_path.glob("peer-*.jsonl")) == 4
    assert FakeRuntime.connects == before_runtime


def test_cross_owner_cannot_set_topic_and_explicit_rename_to_default_wins(codex_env, tmp_path):
    client, service, _ = codex_env
    cid = conversation(client)["id"]
    service.create_user({"username": "member2", "display_name": "member2", "password": PASSWORD, "role": "member"})
    login(client, "member2")
    assert client.get(f"{BASE}/{cid}").status_code == 404
    assert cid not in [c["id"] for c in client.get(BASE).json()]
    assert turn(client, cid, "other-owner-topic", CASES["发票", "预览"]).status_code == 404
    login(client)
    assert home_title(client, cid) == DEFAULT_TITLE
    original = CASES["订单", "导出"]
    assert turn(client, cid, "owned-subject-start", original).status_code == 200
    renamed = client.patch(f"{BASE}/{cid}", json={"title": DEFAULT_TITLE})
    assert renamed.status_code == 200
    replay = turn(client, cid, "owned-subject-start", original)
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert turn(client, cid, "owned-subject-next", CASES["发票", "预览"]).status_code == 200
    assert home_title(client, cid) == DEFAULT_TITLE
    assert sum(len(requests(log_events(log), "turn/start")) for log in tmp_path.glob("peer-*.jsonl")) == 2
