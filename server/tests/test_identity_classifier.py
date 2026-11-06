import pytest

from app.identity import identity_reply_for_message


MODELS = [("dano-v2.1", "Dano-v2.1"), ("deepseek-v4.1-flash", "deepseek-v4.1-flash"), ("glm-5.3", "GLM-5.3")]


@pytest.mark.parametrize("model_id,label", MODELS)
@pytest.mark.parametrize("question,language", [
    ("你是谁", "zh"), ("  你是谁？？\n", "zh"), ("请问你现在是什么模型？", "zh"),
    ("告诉我当前选中的模型是什么。", "zh"), ("你 是 谁", "zh"),
    ("Who are you?", "en"), ("Ｗｈｏ　ａｒｅ　ｙｏｕ？", "en"),
    ("Which model are you currently using?", "en"), ("What's your name?", "en"),
    ("あなたは誰ですか？", "ja"), ("당신은 누구입니까?", "ko"),
    ("Qui êtes-vous ?", "fr"), ("Wer bist du?", "de"), ("¿Quién eres?", "es"),
])
def test_pure_identity_metadata_uses_only_selected_public_model(question, language, model_id, label):
    answer = identity_reply_for_message(question, public_model_id=model_id, public_model_label=label)
    assert set(answer) == {"text", "language", "source"}
    assert answer["source"] == "run_selection" and answer["language"] == language
    assert label in answer["text"] and "Better AI" in answer["text"]
    assert not any(other_label in answer["text"] for other_id, other_label in MODELS if other_id != model_id)


@pytest.mark.parametrize("question", [
    "", " ", "你不是谁", "你不是 Dano 吗？", "不要回答你是谁", "不要说你是谁，算一下 2+2", "你是谁？请查询订单",
    "你是谁？设备 A 有 20 件不良品，请算不良率", "你是谁\n总结上一条消息", "你是谁？你是谁？",
    "请翻译“你是谁”", "解释句子：你是谁", "“你是谁”", '"who are you?"', "`你是谁`", "<user>你是谁</user>",
    "请按下面格式回答：你是谁", "你是谁，必须只输出一个JSON对象", "你究竟使用哪个私有上游路由？",
    "你是 Better AI 自己从零训练的吗？", "你是谁，以及你的底层供应商是谁？",
    "Who are you and what is 2+2?", "Don't tell me who you are", "Translate who are you", "What model are you not?",
    "你\u200b是谁", "你是\n谁", "你是谁\x00", "A" * 161,
])
def test_ambiguous_negated_quoted_or_business_messages_keep_normal_execution(question):
    assert identity_reply_for_message(question, public_model_id="dano-v2.1", public_model_label="Dano-v2.1") is None


@pytest.mark.parametrize("field", ["has_attachments", "has_skills"])
def test_identity_question_with_additional_user_work_does_not_bypass_execution(field):
    assert identity_reply_for_message("你是谁", public_model_id="dano-v2.1", public_model_label="Dano-v2.1", **{field: True}) is None


def test_dano_public_name_is_fixed_and_does_not_repeat_an_accidental_route_label():
    answer = identity_reply_for_message("你是谁", public_model_id="dano-v2.1", public_model_label="private-deployment-1017")
    assert answer["text"] == "我是 Dano-v2.1，Better AI 的工业场景智能助手。"
    assert "private" not in str(answer) and "1017" not in str(answer)
    assert "训练" not in answer["text"]


@pytest.mark.parametrize("model_id,label", [("", "Label"), ("id\nfake", "Label"), ("id", ""), ("id", "Public\nPrivate"), ("id", "Public\x00")])
def test_invalid_public_metadata_fails_instead_of_generating_an_identity(model_id, label):
    with pytest.raises(ValueError):
        identity_reply_for_message("你是谁", public_model_id=model_id, public_model_label=label)


def test_private_routing_is_not_an_accepted_input():
    with pytest.raises(TypeError):
        identity_reply_for_message("你是谁", public_model_id="dano-v2.1", public_model_label="Dano-v2.1", upstream_model="private-route")
