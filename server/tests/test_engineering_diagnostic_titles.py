"""Independent public prompts for familiar maintenance jobs and diagnostics."""
from app.conversation_title import first_message_title


def title(prompt):
    return first_message_title(prompt, engineering=True)


def test_maintenance_topic_preserves_action_and_its_target_across_domains():
    database_backup = title("我需要知道如何备份数据库。")
    file_backup = title("我需要知道如何备份文件。")
    database_rollback = title("数据库出错时要怎样回滚？")
    assert "数据库" in database_backup and "备份" in database_backup
    assert "文件" in file_backup and "备份" in file_backup
    assert "数据库" in database_rollback and "回滚" in database_rollback
    assert len({database_backup, file_backup, database_rollback}) == 3


def test_same_disk_can_be_investigated_for_different_reasons():
    latency = title("磁盘近几天延迟很高，先检查哪项？")
    space = title("磁盘剩余空间不足，先检查哪项？")
    assert "磁盘" in latency and any(w in latency for w in ("延迟", "性能", "耗时"))
    assert "磁盘" in space and any(w in space for w in ("空间", "容量"))
    assert latency != space


def test_an_actual_status_check_keeps_its_own_purpose():
    status = title("帮我核对定时任务现在是否正常。")
    assert "定时任务" in status and any(w in status for w in ("状态", "核对", "核验"))
